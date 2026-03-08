# YuKiKo Agent 强化总计划（不依赖 Plan 模式）

更新时间：2026-03-03  
范围：`app.py`、`core/*`、`services/*`、`plugins/*`、`webui/*`、`config/*`、运行日志  
目标：把当前 Agent 从“可用”提升到“高自治、可控、可扩展、可解释”

---

## 1. 目标定义

### 1.1 最终目标
- Agent 在群聊/私聊中稳定完成复杂任务，不乱跳、不错图、不重复、不泄露内部格式。
- 工具调用由 Agent 自主规划，但必须可审计、可回放、可约束。
- Prompt、触发策略、阈值、下载策略、媒体策略可由人类在 WebUI 完整调参。
- 后续插件能力（例如 Telegram 登录/爬取）可插拔，不需要改核心编排。

### 1.2 非目标（本期不做）
- 暂不直接实现 Telegram 登录/爬取功能本体。
- 暂不追求“无限工具自由执行”，必须保留权限、安全、来源可信约束。

---

## 2. 现状诊断（代码 + 日志）

## 2.1 关键代码体量与复杂度热点
- `core/tools.py`：7189 行
- `core/engine.py`：5048 行
- `core/agent_tools.py`：4446 行
- `app.py`：2316 行
- `core/agent.py`：1773 行

结论：关键能力集中在超大文件，导致规则散落、行为分叉、回归难。

## 2.2 当前最影响体验的 Agent 问题
- 图片问答错图/串图：图像上下文绑定不稳定，候选图来源解释不足。
- “学习表情包”后立刻追问被打断：队列取消策略与不可中断任务边界不稳。
- 引用消息语义误判：机器人知道“在回复谁”，但未可靠知道“用户在追问哪张媒体”。
- 已发送表情后又发失败提示：工具已发图 + final_answer 再带 `image_url` 产生重复发送路径。
- 下载链路不够“官网优先”：找到可下载链接，但来源可信排序和“官方判定”不足。
- 超时后用户感知差：Agent timeout 回退后，上下文连贯性和解释性不足。

## 2.3 配置与 Prompt 侧问题
- Prompt 可热加载，但策略仍有大量硬编码分布在 `agent/engine/tools`。
- WebUI 页面字段为前端静态定义，和后端配置模板演进存在漂移风险。
- 参数虽然多，但缺少“策略组”与“推荐档位”，人类调参成本高。

---

## 3. 根因映射（问题 -> 代码位置）

| 问题 | 直接根因 | 主要位置 |
|---|---|---|
| 错图分析 | 图像候选来源混合（当前消息/引用消息/最近缓存）且缺少“候选解释” | `core/agent_tools.py::_handle_analyze_image`, `core/tools.py::_method_media_analyze_image` |
| 学习后发错图 | 学习任务与后续任务并发/取消边界不清晰，最近表情选择策略受中断影响 | `app.py` 队列提交策略, `core/queue.py`, `core/agent_tools.py::send_emoji/learn_sticker` |
| 引用语义误导 | reply 上下文已提取，但“引用对象=目标媒体”未建立强约束 | `app.py::_resolve_reply_context`, `core/agent.py::_build_user_message` |
| 工具结果与最终回复冲突 | 工具已执行副作用后，LLM 仍可输出多余媒体字段 | `core/agent.py`, `core/engine.py` 结果整合 |
| 下载不够智能 | 官方来源判定规则与 release 发现链不完整 | `core/agent.py` 参数补全, `core/tools.py` 下载流程, 搜索策略 |
| 参数不可控 | UI 字段静态、配置 schema 缺失统一来源 | `webui/src/pages/config.tsx`, `core/webui.py`, `core/config_templates.py` |

---

## 4. 目标架构（Agent 2.0）

## 4.1 四层模型
- Policy 层：安全/权限/来源可信/内容边界（强约束，不交给模型自由发挥）。
- Planner 层：只负责“计划与工具选择”，产出结构化执行计划。
- Executor 层：执行工具 + 副作用写入 + 失败重试 + 证据收集。
- Responder 层：基于执行证据生成用户可读答复，禁止泄露内部协议。

## 4.2 会话状态机（必须落地）
- 状态：`idle -> planning -> tool_running -> synthesizing -> done`
- 附带：`operation_id`、`parent_message_id`、`artifact_refs`
- 规则：同一 `operation_id` 内的副作用工具（上传/学习/发送）默认不可中断。

## 4.3 Artifact 图（解决“这张图是哪张”）
- 每条消息入库媒体索引：`message_id -> [image/video/audio refs]`
- reply 建边：`current_message_id -> replied_message_id`
- analyze/send 类工具必须显式绑定 `target_message_id` 或 `target_artifact_id`
- 当候选>1时，Agent 先澄清，不允许盲选。

---

## 5. Prompt 重构方案

## 5.1 Prompt 拆分
- `agent_policy_prompt`：硬规则（禁泄露、禁伪造、禁空答工具流）
- `agent_planner_prompt`：何时调工具、如何收束步骤
- `agent_responder_prompt`：语言风格、简洁度、对用户解释
- `tool_specific_prompts/*`：下载、图像、搜索、QZone 等专项策略

## 5.2 强制规则
- 输出通道分离：工具调用结构永不走用户回复通道。
- final_answer 禁止携带未授权媒体 URL。
- 需要引用上下文时必须声明证据来源（当前图/引用图/最近图）。

## 5.3 图像问答专用 Prompt 增强
- 必须先判断“目标图绑定是否唯一”。
- 不唯一时固定回复澄清模板，不直接猜。
- 低置信度时不编结论，返回“不确定 + 下一步”。

---

## 6. 参数体系重构（可调可解释）

## 6.1 单一配置 Schema
- 新增统一 schema（建议 `core/config_schema.py`）：
  - 字段类型、默认值、范围、描述、UI 展示分组
  - 后端与 WebUI 共用同一 schema 源

## 6.2 WebUI 改造
- 从后端拉取 schema 自动渲染字段，不再前端硬编码所有 section。
- 每个参数显示：当前值、默认值、推荐值、风险提示。
- 增加“策略档位一键切换”：稳健/均衡/激进。

## 6.3 关键参数分层
- 触发层：`allow_non_to_me`, `undirected_policy`, follow-up window
- 调度层：`cancel_previous_mode`, `interruptible rules`, timeout
- 工具层：download source rank、vision confidence gate、retry policy
- 输出层：verbosity、sanitize、PII redaction

---

## 7. 下载能力升级路线（“官网优先 + 全面”）

## 7.1 三阶段下载决策
1. Discover：搜索候选（官网、官方文档、GitHub Release、应用商店）
2. Verify：来源评分（域名可信、发布主体、签名/哈希、重定向链）
3. Deliver：下载、校验、上传、回执（包含来源说明）

## 7.2 来源评分建议
- P0：官方主域名下载页、官方文档下载链接
- P1：官方 GitHub Release（owner 与项目一致）
- P2：可信分发平台（有历史信誉）
- P3：聚合下载站（默认低优先级）

## 7.3 行为规则
- 若用户要“官网安装包”，必须先返回“来源判定结果 + 下载动作”。
- 若仅找到低可信来源，先提示风险，不默认上传给用户。

---

## 8. 插件与 Skill 扩展（为 Telegram 预留）

## 8.1 插件契约标准化
- 统一插件 manifest：`name`, `capabilities`, `auth_type`, `rate_limit`, `side_effect_level`
- 工具注册时标注权限等级（read/write/network/account）

## 8.2 Skill 学习机制
- 新增 `skill_registry`：Agent 可检索“已有技能 + 适用场景”
- 新 skill 引入流程：发现 -> 审核 -> 启用 -> 灰度
- 训练规则：只允许学习结构化 skill，不学习用户恶意自由文本作为执行策略

## 8.3 Telegram 预留位（本期仅接口）
- `plugins/telegram_bridge/` 预留
- 先定义 capability：`telegram.login`, `telegram.fetch_messages`, `telegram.search`
- 默认关闭，仅超管开启

---

## 9. 实施阶段（按优先级）

## Phase 0（立即，1-2 天）：可观测性补齐
- 为图像分析记录候选来源明细（当前/引用/最近 + message_id）。
- 队列取消日志增加 `interruptible` 与取消原因细分。
- 记录 final_answer 媒体字段来源（tool/result/user_input）。

验收：
- 出现错图时，日志能明确看到“选错的是哪张图、为什么被选中”。

## Phase 1（P0 修复，2-4 天）：正确性兜底
- analyze_image 强制 `target_message_id` 绑定；无绑定且多候选直接澄清。
- `learn_sticker`/`send_emoji` 任务设为不可中断事务。
- 工具已发图后，禁止 final_answer 再附带重复 `image_url`。
- reply 场景加入“引用链目标优先级”硬规则。

验收：
- “学习后立即发刚学表情”成功率 > 95%，无错图。

## Phase 2（P1，4-7 天）：下载智能升级
- 实现来源评分器与官网优先策略。
- 增加 GitHub Release 自动发现与资产过滤。
- 下载回执附来源证据（域名/发布主体/版本）。

验收：
- 用户请求“官网安装包”时，优先命中官方来源，不再随机站点漂移。

## Phase 3（P1，5-7 天）：Prompt 与策略解耦
- Prompt 拆分落地，硬规则从 prompt 下沉到 policy 代码。
- image/question cues、download cues、reference cues 全部移到可编辑配置。

验收：
- 不改代码即可在 WebUI 调整主要行为策略。

## Phase 4（P2，7-10 天）：参数系统统一
- 落地配置 schema + WebUI 动态渲染。
- 补齐默认值展示和“重置默认”能力。

验收：
- WebUI 所见即配置真值，不再出现字段空白或漂移。

## Phase 5（P2，并行）：插件/Skill 基础设施
- 插件能力声明、权限管控、灰度开关。
- Skill registry 与策略学习接口。

验收：
- 新插件可不改核心引擎接入并受统一权限控制。

---

## 10. 关键验收指标（必须量化）

- `wrong_image_binding_rate`（错图率） < 1%
- `sticker_learn_then_send_success_rate` > 95%
- `agent_timeout_rate` < 3%
- `tool_call_leak_rate`（内部协议泄露） = 0
- `official_source_hit_rate`（官网命中率，下载类） > 90%
- `config_ui_coverage`（WebUI 参数覆盖率） = 100%

---

## 11. 风险与回滚策略

- 风险：规则收紧后可能导致“先澄清再执行”次数增加，用户感知变慢。
- 对策：给关键任务（下载/识图）增加“处理中”占位回复，减少“机器人不理我”感。
- 回滚：每个 Phase 独立开关，异常时可回退到上一个稳定策略组。

---

## 12. 立即执行清单（下一步开发顺序）

1. 先做 Phase 0 日志增强（不改业务行为，先拿真相）。
2. 紧接做 Phase 1 的四个硬修（错图、学习中断、重复媒体、引用链）。
3. 完成后再做下载来源评分器（Phase 2）。
4. 最后做 Prompt/参数系统工程化（Phase 3/4）。

这个顺序能最快把“用户体感翻车”降下来，同时为“最强 Agent”留出可持续演进结构。
