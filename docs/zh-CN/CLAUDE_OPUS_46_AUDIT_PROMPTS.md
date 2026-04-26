# Claude Opus 4.6 审计提示词

本文档是给 `Claude Opus 4.6` 用的高强度审计提示词，针对当前 `yukiko-bot/` 仓库定制。

适用项目特征：

- Python 3.11+ 后端
- NoneBot2 + OneBot V11 + NapCat
- React 18 + TypeScript + Vite WebUI
- 大量核心单文件较大，存在并发、路由、安全、工具调用、鉴权、回归风险
- 当前工作区是脏树，不能把所有未提交改动直接当成 bug

## 审计范围

优先关注：

- `app.py`
- `main.py`
- `core/`
- `services/`
- `plugins/`
- `utils/`
- `tests/`
- `webui/src/`

默认忽略：

- `_vendor/`
- `_ext/`
- `NapCat.Shell.Windows.Node/`
- `**/node_modules/`
- `**/__pycache__/`
- 构建产物、缓存、二进制文件

## 推荐使用顺序

1. 先贴“Prompt 01 总控审计”
2. 再根据它的初审结果，贴对应专题 prompt
3. 最后贴“Prompt 10 复审反驳”和“Prompt 11 漏洞回归设计”

## Prompt 01 总控审计

```text
你现在是这个仓库的首席审计工程师，不是讲概念的顾问。你的目标是主动发现真实问题，而不是泛泛给建议。

仓库信息：
- 项目根目录：`yukiko-bot/`
- 技术栈：Python 后端 + React/TypeScript WebUI
- 这是一个 AI QQ 机器人，包含消息路由、群聊并发、会话状态、工具调用、记忆、安全策略、WebUI 管理端、多模型适配
- 当前工作树可能有未提交改动，不能默认把改动本身当成 bug；要靠行为、代码路径、测试、约束不一致来证明问题

你的任务：
1. 先快速阅读仓库结构，建立风险地图。
2. 主动运行你认为必要的只读检查、测试、构建、grep、静态搜索，不要等我提示。
3. 重点找“真实、高价值、可证明”的问题：
   - 逻辑 bug
   - 状态机错误
   - 并发/竞态/取消中断问题
   - 权限绕过
   - WebUI 鉴权缺陷
   - 工具调用参数校验缺陷
   - 配置迁移/默认值导致的行为偏差
   - 回归风险
   - 异常路径漏处理
   - 测试缺口掩盖的脆弱实现
4. 发现疑点后，不要立刻下结论，先主动反证：
   - 有没有其他保护逻辑覆盖？
   - 现有测试是否已经覆盖？
   - 是否只是不优雅但不构成 bug？
5. 只有在你能给出代码证据、触发路径、影响范围时，才把它列为正式 findings。

工作方式要求：
- 优先用 `rg`、阅读关键文件、运行测试、运行 WebUI build。
- 不要只看 `README` 就输出结论。
- 不要停留在“可能有问题”，要尽量追到具体函数、条件分支、状态变量、接口路径。
- 如果一个问题可以复现，给出最短复现步骤。
- 如果一个问题暂时不能复现，也要给出为什么仍然成立的静态推理链。
- 忽略 `_vendor/`、`_ext/`、`NapCat.Shell.Windows.Node/`、`node_modules/`。

建议你主动考虑这些命令是否需要执行：
- `git status --short`
- `rg --files`
- `pytest tests/`
- `pytest -q tests/<suspect_test>.py`
- `npm run build`（在 `webui/`）

输出格式严格如下：

第一部分：审计结论概览
- 用 5 句以内总结风险面和你实际检查了什么

第二部分：Findings
- 只列真实问题，按严重级别排序
- 每条 finding 必须包含：
  - 标题
  - 严重级别：critical / high / medium / low
  - 位置：文件路径 + 行号或函数名
  - 为什么是问题
  - 触发条件
  - 实际影响
  - 证据
  - 是否已有测试覆盖
  - 建议修复方向

第三部分：未证实疑点
- 只列你认为值得继续深挖、但证据还不够闭合的点

第四部分：审计盲区
- 说明你因为环境、依赖、外部服务或权限没能验证的部分

注意：
- Findings 是主要产出，概览只是辅助。
- 不要用大段空话。
- 不要把“可以优化”写成 bug。
- 不要为了凑数量降低标准。
```

## Prompt 02 脏树安全审计

```text
这个仓库当前有大量未提交改动。你做审计时必须遵守下面规则：

1. 不能把“未提交”本身当成问题。
2. 不能因为文件大、改动多，就用保守措辞敷衍。
3. 必须区分：
   - 真正的行为缺陷
   - 重构中间态但当前仍自洽
   - 暂时不优雅但不构成 bug
4. 如果你怀疑某处改动引入回归，必须给出：
   - 改动影响链
   - 旧行为/新行为差异
   - 哪个测试应该失败但没失败，或者目前缺失什么测试
5. 如果你无法证明，就把它放进“未证实疑点”，不要冒充 finding。

请基于这个标准重新审计一次，并且优先看：
- `app.py`
- `core/engine.py`
- `core/agent.py`
- `core/tools.py`
- `core/webui.py`
- `services/model_client.py`
- `webui/src/`
```

## Prompt 03 并发与中断专题

```text
请只做一件事：深挖这个项目里的并发、竞态、取消、重入、状态污染问题。

背景：
- 这是群聊机器人
- 存在 per-group queue、smart interrupt、session state、tool calling、多步 agent loop
- 这种系统最容易出现“上一条任务状态泄漏到下一条”“取消不彻底”“回复串台”“缓存污染”“重复发送”“锁范围错误”

你的审计目标：
- 从消息进入开始，沿着事件流追踪到最终发送
- 找出所有共享状态、可变对象、缓存、异步任务、取消分支、超时分支
- 主动寻找：
  - group queue 并发竞态
  - interrupt/cancel 后残留副作用
  - followup/session 绑定错误
  - tool 调用结果串到错误上下文
  - memory / recall / trigger 状态错配
  - retry / fallback 导致的重复执行

工作要求：
- 必须追踪真实代码路径，不要停留在概念层
- 尽可能用测试或最小复现来验证
- 特别关注：
  - `core/queue.py`
  - `core/trigger.py`
  - `core/engine.py`
  - `core/agent.py`
  - `core/memory.py`
  - `app.py`

输出：
- 先给出并发模型摘要
- 再列 findings
- 每条 finding 都要说明“哪个状态在什么时刻被谁错误共享或错误中断”
```

## Prompt 04 WebUI 鉴权与管理面专题

```text
请把自己当成内部红队，只审计 WebUI 与管理接口。

目标：
- 找认证绕过
- 找 token 校验缺陷
- 找不该暴露的管理能力
- 找前后端状态不一致
- 找敏感信息泄漏
- 找路径保护不完整

重点文件：
- `core/webui.py`
- `core/webui_auth_routes.py`
- `core/webui_cookie_routes.py`
- `core/webui_chat_helpers.py`
- `webui/src/`

必须主动验证：
1. API 路由是否全部一致地做鉴权
2. 鉴权失败时是否真正 fail closed
3. 静态资源、聊天接口、日志接口、配置接口、插件接口、图片生成接口是否有漏网之鱼
4. 前端是否错误假设后端一定已鉴权
5. 是否存在把异常信息、token、路径、内部堆栈暴露给前端的情况

如果你能运行测试，请优先关注：
- `tests/test_webui_auth_regression.py`
- `tests/test_webui_management_regression.py`
- `tests/test_webui_chat_media_regression.py`
- `tests/test_webui_image_gen_route_regression.py`

输出必须是 findings-first，并给出接口路径级别的证据。
```

## Prompt 05 工具调用、权限、Schema 专题

```text
请专门审计 agent tool 体系，不要分散注意力。

背景：
- 这是一个支持多工具调用的 agent 系统
- 有 tool registry、schema、参数别名、权限等级、不同 provider 的工具调用适配
- 这种地方很容易出现“schema 说一套，运行时做另一套”“别名绕过校验”“权限过滤不彻底”“高风险工具泄漏给低权限用户”

重点文件：
- `core/tools.py`
- `core/agent_tools.py`
- `core/agent_tools_registry.py`
- `core/agent_tools_*.py`
- `core/tools_*.py`
- `services/openai_compatible.py`
- `services/model_client.py`

重点测试：
- `tests/test_tool_schema_audit.py`
- `tests/test_tool_registry_smoke.py`
- `tests/test_tool_call_leak_regression.py`
- `tests/test_platform_tool_smoke.py`

请主动找以下问题：
- schema 与 handler 参数不一致
- required/optional 不一致
- alias 导致绕过
- permission gating 不完整
- tool result 序列化异常
- 模型返回异常 tool call 时的 fail-open 行为
- provider 适配层导致的工具调用结构偏差

输出要求：
- 每个 finding 必须写清“低权限主体如何越过预期边界”或“模型如何触发错误工具执行”
```

## Prompt 06 模型适配与故障切换专题

```text
请只审计模型调用链、供应商适配、失败切换逻辑。

背景：
- 项目支持多个模型供应商
- 有 failover、fatal/transient error 区分、provider alias
- 这类逻辑容易出现误切换、重复请求、无限重试、错误吞掉、模型能力假设不一致

重点文件：
- `services/model_client.py`
- `services/base_client.py`
- `services/openai_compatible.py`
- `services/anthropic.py`
- `services/gemini.py`
- 以及被这些代码直接调用的上层入口

重点关注：
- fatal vs transient 分类是否可靠
- failover 是否会破坏幂等性
- fallback 后参数是否仍合法
- tool calling / thinking / multimodal 标志位是否在各 provider 间错配
- 错误路径是否可能返回半成品结果
- quota / timeout / cancel 的边界条件是否会污染会话状态

如果能跑测试，优先看：
- `tests/test_model_client_failover_regression.py`
- `tests/test_weak_model_protection.py`
- `tests/test_thinking_engine_regression.py`

不要泛泛而谈“多供应商复杂”。我要的是具体 bug。
```

## Prompt 07 安全策略、内容守卫、误触发专题

```text
请审计这个项目的 safety / trigger / router 协作逻辑。

目标：
- 找误判导致的过度回复
- 找高风险内容漏拦截
- 找 reply / mention / followup 判断错误
- 找 safety 和 router 的边界缝隙
- 找 prompt review / post review / local guardrail 的 fail-open 行为

重点文件：
- `core/safety.py`
- `core/router.py`
- `core/trigger.py`
- `core/prompt_policy.py`
- `core/system_prompts.py`
- `core/engine.py`
- `config/templates/master.template.yml`

重点测试：
- `tests/test_high_risk_and_sticker_regression.py`
- `tests/test_high_risk_ban_guard_regression.py`
- `tests/test_safety_profile_regression.py`
- `tests/test_local_intent_heuristic_regression.py`
- `tests/test_router_media_fallback_regression.py`

要求：
- 逐步说明一个输入是如何穿过 router、trigger、safety 的
- 找配置默认值与运行时实现不一致的地方
- 区分“体验不好”和“安全缺陷”
```

## Prompt 08 记忆、知识、学习污染专题

```text
请只审计记忆、知识库、学习能力，不讨论别的。

目标：
- 找错误记忆写入
- 找跨群/跨会话污染
- 找记忆召回与隐私边界问题
- 找学习机制导致的 prompt/data 污染
- 找知识注入后对回答约束失效的情况

重点文件：
- `core/memory.py`
- `core/knowledge.py`
- `core/knowledge_updater.py`
- `plugins/self_learning.py`
- `core/enhanced_recall.py`
- `core/engine.py`

重点测试：
- `tests/test_group_memory_knowledge_integration_regression.py`
- `tests/test_learning_guard_regression.py`
- `tests/test_self_learning_plugin.py`
- `tests/test_relationship_humanization_regression.py`

要求：
- 不要只说“可能存在记忆泄漏”
- 必须给出：
  - 写入入口
  - 读取入口
  - 隔离边界
  - 哪个边界失效
```

## Prompt 09 差异优先审计

```text
请先看当前工作树改动，再做差异优先审计。

规则：
1. 先用 `git status --short` 和必要的 diff/文件阅读确定改动热点。
2. 优先审计这些热点文件，因为它们最可能引入新回归。
3. 但不能只看 diff；如果改动触发了跨模块影响，要继续沿调用链深挖。
4. 对每个 finding，注明它更像是：
   - 新引入回归
   - 老问题仍然存在
   - 测试缺失导致的潜伏问题

优先关注当前仓库可能正在重构的大文件：
- `app.py`
- `core/agent.py`
- `core/engine.py`
- `core/tools.py`
- `core/webui.py`
- `services/model_client.py`
- `services/openai_compatible.py`
- `webui/src/pages/`

输出必须明确哪些结论依赖 diff，哪些来自整体行为分析。
```

## Prompt 10 复审反驳

```text
请扮演一个非常苛刻的第二审计员，专门反驳你自己刚才的 findings。

你的任务不是找新问题，而是检查旧结论有没有夸大、误判、证据不足。

对每一条 finding：
1. 尝试寻找保护逻辑、上游约束、下游兜底。
2. 检查是否已有测试覆盖并且其实证明它不是 bug。
3. 检查复现条件是否过于理想化，现实中不成立。
4. 检查影响范围是否被夸大。
5. 如果反驳失败，再保留这条 finding。

输出格式：
- 保留的 findings
- 被降级的 findings
- 被撤销的 findings
- 仍需人工确认的 findings

要求：
- 不要为了一致性强行维持原结论。
- 也不要为了“平衡”强行推翻正确结论。
- 目标是让最后留下的问题更硬、更难反驳。
```

## Prompt 11 为每个 finding 设计回归测试

```text
基于你已经确认的 findings，为每一个问题设计最小回归测试方案。

要求：
- 优先写 pytest 思路；如果是前端则写最小可执行验证思路
- 说明应该放在哪个测试文件，或者是否需要新建测试
- 说明断言点
- 说明这个测试为什么能锁住该问题而不是只锁实现细节
- 如果一个 finding 很难自动化，说明原因并给手工验证脚本

输出格式：
- finding 标题
- 建议测试位置
- 测试思路
- 核心输入
- 核心断言
- 为什么这个测试足够好
```

## Prompt 12 直接逼它自己动手验证

```text
这次你不能只做静态代码阅读。你必须主动想办法验证自己的怀疑。

执行要求：
1. 先列出你准备验证的 5 个最高价值假设。
2. 对每个假设，明确你打算怎么验证：
   - 跑现有测试
   - 缩小范围重跑
   - build WebUI
   - 搜索调用链
   - 构造最小输入
   - 检查异常路径
3. 每完成一个验证，就更新结论：
   - confirmed
   - weakened
   - disproved
   - blocked
4. 最后只保留被 confirmed 或高可信静态证明支撑的问题。

输出要求：
- 不要一次性下结论
- 必须展示你的“怀疑 -> 验证 -> 修正判断”过程
- 但最终汇报仍然要精炼，以 findings 为主
```

## Prompt 13 要它自己找命令、自己分批测试

```text
请你自己制定审计执行计划，并主动分批运行命令，不要把命令选择权丢回给我。

约束：
- 先做低成本高收益检查，再做重检查
- 不要一上来全量乱跑；要根据代码热点和测试命名做选择
- 如果全量测试失败，要把失败拆成环境问题、已知缺依赖、真实回归 三类

你至少要考虑是否运行：
- `pytest tests/`
- 单测文件级 rerun
- `npm run build`（`webui/`）
- `rg` 搜索调用链

输出：
- 审计执行轨迹
- 每一步为什么值得做
- 哪些命令结果真正改变了你的判断
```

## Prompt 14 输出风格约束

```text
你的输出必须符合下面风格，不允许写成泛泛的代码评审废话：

1. Findings 优先，概览靠后。
2. 每条 finding 都要带代码位置。
3. 不要出现“建议进一步检查”“可能需要关注”这种空话，除非你明确把它放进“未证实疑点”。
4. 不要罗列无关优化建议。
5. 不要把“代码风格、命名、文件太大”当成主要 findings，除非它直接导致行为错误。
6. 如果没有发现高质量问题，要明确说“本轮没有发现可证明的高置信问题”，并说明仍然存在的审计盲区。
```

## 一次性终极组合 Prompt

如果你只想贴一次，就直接贴下面这段：

```text
你现在对 `yukiko-bot/` 做一次高强度工程审计。你不是来提优化建议的，你是来找真实 bug、真实回归、真实权限/状态/鉴权问题的。

项目特征：
- Python 后端 + React/TypeScript WebUI
- AI QQ 机器人，涉及消息路由、群聊并发、会话状态、工具调用、记忆、安全策略、WebUI 管理、多模型适配
- 当前工作树可能是脏树，不能把未提交改动本身当成 bug

审计范围：
- 优先看 `app.py`、`main.py`、`core/`、`services/`、`plugins/`、`utils/`、`tests/`、`webui/src/`
- 忽略 `_vendor/`、`_ext/`、`NapCat.Shell.Windows.Node/`、`node_modules/`、`__pycache__/`

你的行为要求：
1. 先快速建立风险地图，再开始深挖。
2. 主动运行必要命令，不要等我指定：
   - `git status --short`
   - `rg --files`
   - 合适的 `pytest`
   - `npm run build`（在 `webui/`）
3. 先从改动热点和高风险模块入手：
   - `app.py`
   - `core/engine.py`
   - `core/agent.py`
   - `core/tools.py`
   - `core/webui.py`
   - `core/queue.py`
   - `core/trigger.py`
   - `core/safety.py`
   - `services/model_client.py`
   - `services/openai_compatible.py`
4. 重点找：
   - 并发/竞态/取消不彻底
   - 群聊状态串台
   - 工具调用 schema 与运行时不一致
   - 权限边界缺陷
   - WebUI 鉴权漏口
   - failover / fallback 回归
   - 配置默认值与运行时行为不一致
   - 记忆/学习污染
   - 高风险内容保护的 fail-open
5. 每发现一个问题，先尝试反驳自己：
   - 有没有现有保护逻辑？
   - 有没有现有测试覆盖？
   - 这个问题是否只是丑陋而非错误？
6. 如果能复现就给最短复现；不能复现就给静态推理链。
7. 最后再做一轮自我复审，撤掉证据不足的问题。

输出格式：

第一部分：你实际执行了哪些检查

第二部分：Findings
- 只列真实问题，按严重级别排序
- 每条包含：
  - 标题
  - 严重级别
  - 位置
  - 触发条件
  - 为什么成立
  - 影响
  - 证据
  - 是否已有测试覆盖
  - 修复方向

第三部分：未证实疑点

第四部分：审计盲区

第五部分：你撤销或降级了哪些原始怀疑，为什么

风格要求：
- findings-first
- 不说空话
- 不凑数量
- 不把优化建议伪装成 bug
- 没有高质量问题就明确说没有
```

## 使用建议

- 如果第一次审计太散，第二轮直接贴专题 prompt，不要继续用总控 prompt。
- 如果它开始输出“可能”“建议关注”“可以优化”，立刻补贴 `Prompt 14 输出风格约束`。
- 如果它只做静态阅读不跑命令，补贴 `Prompt 12` 和 `Prompt 13`。
- 如果它找到很多低质量问题，补贴 `Prompt 10` 让它自己反驳。
- 如果你准备让它顺手补测试，最后再贴 `Prompt 11`。

## 2026-04-24 第二轮复审版

这一节用于“第一轮已经有人扫过、而且仓库里已经发生过修复”的场景。

适用情况：

- 你已经拿到过一版安全审计报告
- 其中一部分问题已经在当前工作树修复
- 你不希望 Claude 重复报已经收口或已经被代码覆盖的点
- 你希望它把精力放在“复核旧结论 + 继续深挖剩余问题”上

### 当前树状态快照

基于当前工作树，可先把问题分成三类：

1. **本轮已收口**
   - Setup 模式已增加临时 `setup_token` + HttpOnly setup session 访问门槛
   - Setup 默认强制回退到 `127.0.0.1`
   - WebUI 主登录流已改成 cookie session 优先，不再把 `WEBUI_TOKEN` 持久化到浏览器可读存储

2. **当前代码里已经有防护，不应机械复报**
   - Setup SPA 静态文件访问已使用 `resolve()` + 前缀校验
   - SQLite 上传校验已拒绝 `trigger`
   - Admin update passthrough 已做白名单过滤

3. **仍值得继续深挖**
   - WebUI 登录限速仍是内存级实现，且对代理/持久化不敏感
   - SecretManager 在 Windows 上的权限收紧能力较弱
   - 解密失败返回空字符串的降级语义可能掩盖配置错误
   - 数据库枚举/解析逻辑是否暴露过宽
   - WebUI 其他路由是否还有隐性 token 依赖或 session 边界缺口
   - 更高价值的主战场仍然是：并发、群聊状态污染、工具权限边界、记忆污染、模型 failover 回归

### 第二轮推荐顺序

1. 先贴 `Prompt 15 当前树复审约束`
2. 再贴 `Prompt 16 剩余风险深挖`
3. 如果它又报出旧问题，立刻贴 `Prompt 17 禁止复读误报`
4. 最后贴 `Prompt 18 修复后复验`

## Prompt 15 当前树复审约束

```text
你现在做的是第二轮安全复审，不是从零开始扫库。

当前工作树已经发生过一轮修复。你必须遵守下面约束：

1. 不要机械重复旧报告。
2. 如果一个问题你准备再次上报，必须先核对当前代码是否已经收口。
3. 如果当前代码已经加入明确防护，而你仍认为问题成立，你必须指出：
   - 新的绕过方式
   - 旧防护为何不够
   - 影响为何依旧真实
4. 如果你做不到，就把它撤销，不要重复报。

你尤其不能不经核对就重复上报下面这些旧点：
- Setup 无认证
- Setup SPA 路径穿越
- SQLite trigger 导入
- Admin update passthrough 参数注入
- WebUI token 持久化在浏览器可读存储

你必须主动检查当前实现，而不是沿用旧结论。

输出要求：
- 第一部分：被当前代码证伪或已收口的旧问题
- 第二部分：仍然成立的问题
- 第三部分：你发现的真正新增问题
```

## Prompt 16 剩余风险深挖

```text
请基于当前工作树，只深挖“仍可能真实成立”的剩余风险，不要重复已经收口的旧问题。

优先级：
1. WebUI 登录限速与认证边界
2. SecretManager / 配置密钥失败语义
3. 数据库访问面与文件解析边界
4. 群聊并发、会话状态污染、取消不彻底
5. 工具调用权限边界
6. 记忆/学习污染
7. 多模型 failover 回归

重点文件：
- `core/webui_auth_routes.py`
- `core/crypto.py`
- `core/webui.py`
- `core/queue.py`
- `core/trigger.py`
- `core/engine.py`
- `core/agent.py`
- `core/tools.py`
- `core/memory.py`
- `plugins/self_learning.py`
- `services/model_client.py`

重点要求：
- 不要因为旧报告里提过就默认成立
- 不要把硬化建议写成漏洞
- 对每一个 finding，都要说明它和“已修掉的旧问题”不是同一个东西

输出格式：
- findings
- 已排除旧项
- 仍需人工确认的疑点
```

## Prompt 17 禁止复读误报

```text
你上一轮输出里如果再次出现下列老结论，请逐条自查并撤销，除非你能给出新的绕过证据：

1. “Setup 完全无认证”
2. “Setup SPA 仍然可以直接 .. 路径穿越”
3. “SQLite trigger 导入未防护”
4. “Admin update 任意 passthrough 参数可直接注入”
5. “WebUI token 持久化在 localStorage/sessionStorage”
6. “没有配置 CORS 所以一定存在跨域漏洞”

注意第 6 条：
- “没配 CORS” 本身不是漏洞
- 你必须基于当前 cookie、same-site、认证方式、请求类型、浏览器行为来证明真实可利用性
- 如果做不到，就不要把它列为 finding

请输出：
- 撤销的错误结论
- 保留的高置信结论
- 你新增发现的更强问题
```

## Prompt 18 修复后复验

```text
请把自己当成补丁验证工程师，而不是扫库工程师。

目标：
- 验证最近的安全修复有没有真的把洞堵住
- 同时找这些修复是否引入了新回归

你需要重点验证：
1. Setup 只有持有临时 `setup_token` / setup session 的访问者才能访问
2. Setup token 是否能正确换成 cookie session
3. Setup 静态资源访问是否也受保护
4. WebUI 登录后是否只依赖 HttpOnly cookie 就能工作
5. 前端是否还残留任何对 `webui_token` 持久化存储或直接读取
6. 登录、登出、重启、插件、Cookie 管理页是否还能正常工作

建议主动运行：
- `pytest -q tests/test_webui_auth_regression.py tests/test_webui_setup_auth_regression.py`
- `pytest -q tests/test_webui_chat_media_regression.py tests/test_webui_env_regression.py tests/test_webui_image_gen_route_regression.py tests/test_webui_management_regression.py tests/test_image_provider_adapter_regression.py`
- `npm run build`（在 `webui/`）
- `rg -n "webui_token|sessionStorage|getToken\\(|Authorization: \`Bearer \\\$\\{api\\.getToken"` webui/src`

输出：
- 已确认有效的修复
- 修复引入的回归
- 仍然可绕过的点
- 测试盲区
```

## Prompt 19 只做剩余高价值攻击面

```text
不要再把时间花在已经修过的 WebUI setup/token 老问题上。

请直接把精力投入这 5 个更高价值的攻击面：
- 群聊并发与状态串台
- Agent 工具权限边界
- 记忆/自学习污染
- 模型 failover / provider 适配回归
- 高风险内容 guardrail 的 fail-open

要求：
- 每个攻击面至少给出 1 个你真正验证过的高价值结论，或者明确说明本轮未发现高置信问题
- 不要用旧问题凑数
- 优先跑现有回归测试并沿失败/热点调用链继续深挖
```

## Prompt 20 一次性第二轮组合 Prompt

如果你现在已经有一份旧报告，而且仓库里已经修过一轮，就直接贴下面这段：

```text
你现在做的是 `yukiko-bot/` 的第二轮安全复审。

注意：这不是从零开始扫库。当前工作树已经修过一轮，所以你必须先复核旧结论，再决定哪些问题仍然成立。

你不能不经检查就重复上报下面这些旧点：
- Setup 无认证
- Setup SPA 路径穿越
- SQLite trigger 导入
- Admin update passthrough 参数注入
- WebUI token 持久化在浏览器可读存储
- “无 CORS 所以必然存在漏洞”

如果你认为其中任何一点现在仍然成立，你必须给出：
- 当前代码位置
- 新的绕过方式
- 为什么现有防护仍然不够
- 最短复现路径

本轮你应优先深挖：
- `core/webui_auth_routes.py`
- `core/crypto.py`
- `core/webui.py`
- `core/queue.py`
- `core/trigger.py`
- `core/engine.py`
- `core/agent.py`
- `core/tools.py`
- `core/memory.py`
- `plugins/self_learning.py`
- `services/model_client.py`

重点找：
- 登录限速边界缺陷
- SecretManager 失败语义与权限问题
- 数据库访问面过宽
- 群聊并发 / 取消 / 状态污染
- 工具权限边界
- 记忆污染
- 模型 failover 回归
- 高风险 guardrail fail-open

你必须主动运行必要验证，而不是只做静态阅读。优先考虑：
- 精准 `pytest`
- `npm run build`
- `rg` 搜索调用链
- 对已修补点做补丁复验

输出格式：

第一部分：已被当前代码证伪或已收口的旧问题

第二部分：本轮仍成立的 findings
- 每条必须包含：标题、严重级别、位置、触发条件、影响、证据、是否已有测试覆盖、修复方向

第三部分：新增 findings

第四部分：仍需人工确认的疑点

第五部分：你做过的验证

风格要求：
- findings-first
- 不复读旧报告
- 不把硬化建议写成漏洞
- 没有高置信问题就明确说没有
```
