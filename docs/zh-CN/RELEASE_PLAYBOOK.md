# YuKiKo 发布与运维终极手册（简体中文）

> 目标：给发布负责人、运维值班、开发同学一份可以直接执行的“发布即作战”文档。
> 
> 适用版本：YuKiKo 当前主干（含 `install.sh`、`bootstrap.sh`、`yukiko` 管理命令、systemd 模板）。

---

## 0. 阅读方式与使用原则

- 先看第 1 章与第 2 章，确认发布目标与边界。
- 首次上生产，按第 3 章到第 7 章顺序执行。
- 日常升级，优先执行第 8 章（更新）与第 9 章（回滚）。
- 遇到异常，先跳第 10 章（故障诊断矩阵），再看第 11 章（日志定位脚本）。
- 发布前务必执行第 12 章“最终自检清单”。
- 不要跳过“已知限制”章节，避免不必要期望。

### 0.1 本手册不承诺的内容

- 不承诺第三方平台（QQ/站点/CDN/支付渠道）100%稳定。
- 不承诺外部下载站点始终存在直链。
- 不承诺 OneBot 上游（NapCat/反向代理）永远不掉线。
- 不承诺“零故障”；本手册提供的是“可恢复、可回滚、可追踪”的工程方法。

### 0.2 发布口径建议

- 对外口径：功能可用、监控完善、发生故障可快速回滚。
- 对内口径：每次发布必须可复现、可验证、可撤销。

---

## 1. 系统概览（发布视角）

### 1.1 关键模块

- `main.py`：服务启动入口。
- `app.py`：NoneBot 事件接入、队列投递、最终发送。
- `core/agent.py`：Agent 推理流程、工具调用、fallback 结果。
- `core/webui.py`：WebUI API（会话、历史、状态、日志流）。
- `webui/`：前端控制台（聊天、配置、插件、日志）。
- `scripts/deploy.py`：Python 依赖部署与运行辅助。
- `install.sh`：Linux 一键安装器（交互/非交互）。
- `bootstrap.sh`：GitHub 远程拉取 + 调起安装器。
- `scripts/yukiko_manager.sh`：统一 `yukiko` 运维命令。

### 1.2 运行时依赖

- Python 3.10+（建议 3.11 或 3.12）。
- Node.js 18+ 与 npm（WebUI 构建）。
- ffmpeg（音视频能力相关）。
- OneBot V11 上游（例如 NapCat）。
- systemd（Linux 托管建议）。

### 1.3 典型数据流

1. OneBot 推送消息到 YuKiKo。
2. `app.py` 做触发判断与会话归并。
3. `core/agent.py` 调度工具与模型推理。
4. `app.py` 分片发送回复并写入会话历史。
5. `core/webui.py` 提供 WebUI 会话/状态读取。

---

## 2. 发布前冻结策略

### 2.1 分支与提交规范

- 发布前至少冻结 1 次合并窗口（建议 2~6 小时）。
- 发布分支只允许“修复型提交”，禁止功能新增。
- 每个提交必须有明确范围（后端/前端/脚本/文档）。
- 若工作区已有脏改动，先整理再发版，避免混入无关内容。

### 2.2 版本标识建议

- 建议使用 tag：`vYYYY.MM.DD-N`。
- 建议在发布说明记录 commit hash、构建时间、执行人。
- 重大改动需附回滚说明和风险评估。

### 2.3 变更分类

- A 类：运行时逻辑（`app.py` / `core/*.py`）。
- B 类：前端显示与可操作性（`webui/src/*`）。
- C 类：部署运维脚本（`install.sh` / `yukiko_manager.sh`）。
- D 类：文档与模板（`docs/*` / `config/templates/*`）。

---

## 3. Linux 一键部署（本地代码仓库模式）

### 3.1 最简交互式部署

```bash
bash install.sh
```

安装器会：

- 检查 Linux 环境与包管理器。
- 安装系统依赖（Python / Node / npm / ffmpeg / git / curl）。
- 检查 Node 版本，不足时尝试升级。
- 写入 `.env` 中 `HOST`、`PORT`、`WEBUI_TOKEN`。
- 执行 Python 环境部署和 WebUI 构建。
- 可选注册 systemd 服务。
- 可选放行防火墙端口。

### 3.2 非交互式部署（适合自动化）

```bash
bash install.sh --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
```

### 3.3 常用参数

- `--host <host>`：写入 `.env` 监听地址。
- `--port <port>`：写入 `.env` 监听端口。
- `--webui-token <token>`：写入 `.env` 的 WebUI token。
- `--service-name <name>`：systemd 服务名。
- `--no-service`：跳过 systemd。
- `--open-firewall`：尝试放行端口。
- `--skip-webui-build`：跳过前端构建（不建议发布使用）。
- `--skip-cli-install`：不安装 `/usr/local/bin/yukiko`。

---

## 4. GitHub 远程脚本直装（无本地仓库预置）

### 4.1 一条命令启动

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh)
```

### 4.2 透传安装参数

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh) -- --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko --open-firewall
```

### 4.3 指定仓库、分支、目录

```bash
bash bootstrap.sh --repo-url https://github.com/dwgx/YuKiKo.git --branch main --install-dir /opt/YuKiKo -- --non-interactive --port 18081
```

### 4.4 bootstrap 行为说明

- 若目标目录存在 git 仓库，会执行 fetch/checkout/pull。
- 若目标目录非空且非仓库，默认清空后重拉。
- 使用 `--keep-existing` 可阻止自动删除非空目录。
- 最终由 `bootstrap.sh` 调起 `install.sh`。

---

## 5. 运维总入口 `yukiko` 命令

### 5.1 帮助与命令总览

```bash
yukiko --help
```

### 5.2 常用动作

```bash
yukiko install --host 0.0.0.0 --port 18081
yukiko update --check-only
yukiko update --restart
yukiko start
yukiko stop
yukiko restart
yukiko status
yukiko logs --lines 200
yukiko register --service-name yukiko --user $USER
yukiko unregister --service-name yukiko
yukiko set-port --host 0.0.0.0 --port 8088
yukiko uninstall --purge-runtime --purge-env
```

### 5.3 `yukiko update` 关键行为

- `git fetch` 获取远程变化。
- 计算 ahead/behind/dirty 状态。
- 默认工作区脏时拒绝更新（可 `--allow-dirty`）。
- 可选同步 Python 依赖。
- 可选构建 WebUI。
- 可选自动重启服务。

---

## 6. 服务托管（systemd）

### 6.1 注册服务

```bash
yukiko register --service-name yukiko --user $USER --enable-now
```

### 6.2 查看状态与日志

```bash
yukiko status
yukiko logs --lines 200
sudo journalctl -u yukiko -f
```

### 6.3 停止/禁用/注销

```bash
yukiko stop
yukiko unregister --service-name yukiko
```

### 6.4 模板文件位置

- 模板：`deploy/systemd/yukiko.service.template`。
- 实际服务：`/etc/systemd/system/<service>.service`。

---

## 7. 完美卸载与残留治理

### 7.1 标准卸载（保留运行时）

```bash
yukiko uninstall --service-name yukiko --yes
```

### 7.2 完整卸载（删除运行时与环境）

```bash
yukiko uninstall --service-name yukiko --purge-runtime --purge-env --yes
```

### 7.3 卸载后核验

- `systemctl status yukiko` 应不存在或为 not-found。
- `/etc/systemd/system/yukiko.service` 应已删除。
- 需要时删除 `/usr/local/bin/yukiko`。
- 需要时删除仓库目录本体。

---

## 8. 发布流程（建议 SOP）

### 8.1 发布前 24 小时

- 冻结高风险改动。
- 收敛未完成需求，明确“不进本次发布”的清单。
- 确认 OneBot 上游与网络环境可用。
- 准备回滚窗口与负责人。

### 8.2 发布前 2 小时

- 拉取最新代码并确认无冲突。
- 执行 Python 语法编译与测试。
- 执行 WebUI 构建检查。
- 记录当前线上 commit 与配置快照。

### 8.3 发布执行

1. `git pull --ff-only`。
2. `yukiko update --restart` 或手工更新。
3. `yukiko status` 验证服务在线。
4. 进入 WebUI 执行冒烟验证。
5. 验证 QQ 群内收发与工具链路。

### 8.4 发布后 30 分钟

- 重点观察 `agent_max_steps`、`send_final`、`queue_final`。
- 关注 `/api/webui/chat/conversations` 与 `/api/webui/chat/history` 返回码。
- 关注 OneBot 心跳与连接重建。

---

## 9. 回滚流程（必须可执行）

### 9.1 触发条件建议

- 持续 5 分钟以上无法回复或回复错误率高。
- WebUI 主功能不可用且无法快速修复。
- 关键工具调用异常导致业务中断。

### 9.2 回滚步骤

```bash
cd /path/to/YuKiKo
git fetch --all --tags
git checkout <last_stable_tag_or_commit>
python scripts/deploy.py
cd webui && npm install && npm run build && cd ..
yukiko restart
```

### 9.3 回滚后验证

- 核对 `git rev-parse --short HEAD` 与目标版本一致。
- 发送 3 类测试消息：普通聊天、工具调用、下载请求。
- WebUI 页面刷新后会话列表与历史可正常读取。

---

## 10. 已知限制与正确预期

### 10.1 下载类请求

- 下载站常见“按钮跳转页”不是文件本体。
- `smart_download` 会校验 content-type 与文件头，发现不匹配会拦截。
- 站点防盗链/验证码/动态脚本可能阻断直链提取。
- 这不是“坏掉”，是安全拦截生效。

### 10.2 搜索类请求

- 通用搜索会出现噪声结果，需多步收敛。
- 若 `max_steps` 过小，可能在逼近答案前提前停止。
- 建议针对下载任务提供更明确站点约束。

### 10.3 WebUI 状态轮询

- OneBot 未连接时，部分接口可能临时返回异常或空数据。
- 前端应对失败做退避与降噪提示。
- Thinking 展示与会话选择可能存在时序差，需结合 trace 排查。

---

## 11. 故障诊断矩阵（现象 -> 排查 -> 处理）

| 现象 | 首看日志关键词 | 首要排查点 | 处理建议 |
| --- | --- | --- | --- |
| WebUI 会话列表失败 | `/api/webui/chat/conversations` | OneBot 是否在线、后端接口返回码 | 先确认 OneBot 连接，再看 webui 接口异常栈 |
| 历史消息加载失败 | `/api/webui/chat/history` | peer_id、bot_id、连接状态 | 校验请求参数和 OneBot 在线状态 |
| Thinking 不显示 | `agent_tool_call`/`queue_submit` | 前端 trace 匹配、轮询节奏 | 打开全局 trace 展示，检查状态轮询 |
| 下载失败（HTML） | `smart_download ok=False` | 目标链接是否真实文件 | 调整抓取步骤，定位真实下载地址 |
| 下载扩展名不匹配 | `expected=.apk` | 实际 URL 与文件头 | 更正 `prefer_ext` 与来源链接 |
| 工具步数耗尽 | `agent_max_steps` | 任务复杂度/检索噪声 | 增加步数或优化任务提示词 |
| 消息收到了但不回 | `trigger_guard_runtime` | to_me、监听策略、触发阈值 | 检查触发守卫配置与群消息策略 |
| 服务频繁重启 | systemd restart count | 进程异常退出原因 | 看 Python 异常栈与资源占用 |
| cookie 失效 | `cookie_expired` | 平台登录态 | 按提示重新登录对应平台 |

---

## 12. 日志关键词速查

- `queue_submit`：消息已入队。
- `agent_timeout_budget`：本次推理超时预算。
- `agent_tool_call`：工具调用开始。
- `agent_tool_result`：工具调用结果。
- `agent_max_steps`：达到最大步数上限。
- `agent_done`：Agent 会话结束。
- `send_final`：最终消息发送状态。
- `queue_final`：队列收尾状态。
- `qq_meta`：OneBot 心跳/生命周期事件。
- `cookie_expired`：cookie 过期提醒。

---

## 13. 发布前“硬性自检命令”

### 13.1 Python 编译检查

```bash
python -m py_compile app.py main.py core/agent.py core/webui.py core/engine.py core/tools.py
```

### 13.2 测试套件

```bash
pytest -q
```

### 13.3 WebUI 构建

```bash
npm --prefix webui run build
```

### 13.4 脚本可用性

```bash
bash install.sh --help
bash bootstrap.sh --help
bash scripts/yukiko_manager.sh --help
```

### 13.5 命令行入口

```bash
bash yukiko --help
```

---

## 14. 发布后冒烟用例（建议至少 15 条）

1. WebUI 登录页可访问。
2. 配置页可读取核心配置。
3. 插件页可展示插件状态。
4. 聊天页可加载会话列表。
5. 聊天页可加载历史消息。
6. Thinking 状态可出现且可消失。
7. 群内 @Bot 普通问候可回复。
8. 非 @ 触发策略符合配置。
9. 工具调用日志可见。
10. 下载任务失败时有明确原因。
11. 发送大段文本分片正常。
12. OneBot 重连后服务恢复正常。
13. `yukiko status` 输出正常。
14. `yukiko logs --lines 50 --no-follow` 可读。
15. systemd 重启后服务自动拉起。

---

## 15. 安全加固建议

- 修改默认 `WEBUI_TOKEN`，长度至少 32。
- 反向代理层增加来源 IP 限制。
- 不要把 `.env` 推到公共仓库。
- 日志脱敏，避免泄露手机号/邮箱/密钥。
- 生产环境限制 shell 与部署权限。
- 使用独立 Linux 用户运行服务。
- 关键脚本执行前做校验和确认。

---

## 16. 性能与稳定性建议

- 队列并发不宜盲目拉满，先压测后调参。
- 下载任务建议单独限流，避免占满推理资源。
- WebUI 轮询要有退避，减少离线时的错误噪声。
- 建议保留最近 7~14 天日志用于回溯。
- 若群规模大，优先优化触发策略减少无效推理。

---

## 17. 运行手册扩展：命令配方（可复制）

### 17.1 只检查远程更新，不执行

```bash
yukiko update --check-only
```

### 17.2 带重启升级

```bash
yukiko update --restart
```

### 17.3 工作区有本地改动也要升级（谨慎）

```bash
yukiko update --allow-dirty --restart
```

### 17.4 仅改端口

```bash
yukiko set-port --host 0.0.0.0 --port 18081
yukiko restart
```

### 17.5 查看最近 300 行日志并持续跟踪

```bash
yukiko logs --lines 300
```

### 17.6 仅查看不跟踪

```bash
yukiko logs --lines 300 --no-follow
```

### 17.7 完整重装（保留代码目录）

```bash
yukiko uninstall --purge-runtime --purge-env --yes
bash install.sh --non-interactive --host 0.0.0.0 --port 18081 --service-name yukiko
```

---

## 18. 最终发布核对清单（超详细）

> 使用方式：逐项打勾，未勾选项必须给出豁免理由。

### A. 代码与提交质量

- [ ] CHK-001 确认 CHK-001：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-002 确认 CHK-002：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-003 确认 CHK-003：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-004 确认 CHK-004：回滚条件与回滚负责人已明确。
- [ ] CHK-005 确认 CHK-005：上线窗口与观察窗口已排期。
- [ ] CHK-006 确认 CHK-006：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-007 确认 CHK-007：关键配置变更已做备份。
- [ ] CHK-008 确认 CHK-008：新加配置项具备默认值或兼容策略。
- [ ] CHK-009 确认 CHK-009：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-010 确认 CHK-010：发布说明包含 commit、时间、执行人。
- [ ] CHK-011 确认 CHK-011：Python 编译检查已通过。
- [ ] CHK-012 确认 CHK-012：测试套件已执行并记录结果。
- [ ] CHK-013 确认 CHK-013：WebUI 构建已通过。
- [ ] CHK-014 确认 CHK-014：systemd 服务控制命令可用。
- [ ] CHK-015 确认 CHK-015：远程 bootstrap 安装流程已验证。
- [ ] CHK-016 确认 CHK-016：`yukiko --help` 输出符合预期。
- [ ] CHK-017 确认 CHK-017：WebUI 登录、会话、历史接口可用。
- [ ] CHK-018 确认 CHK-018：Thinking 流显示在目标场景可见。
- [ ] CHK-019 确认 CHK-019：下载任务失败时有明确拦截原因。
- [ ] CHK-020 确认 CHK-020：OneBot 重连后队列与会话无异常。
- [ ] CHK-021 确认 CHK-021：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-022 确认 CHK-022：错误日志不会暴露敏感密钥。
- [ ] CHK-023 确认 CHK-023：生产 token 已替换为随机高强度值。
- [ ] CHK-024 确认 CHK-024：CPU/内存占用在可接受范围。
- [ ] CHK-025 确认 CHK-025：异常场景应答路径已演练。
- [ ] CHK-026 确认 CHK-026：发布后监控责任人已就位。
- [ ] CHK-027 确认 CHK-027：值班沟通渠道已确认。
- [ ] CHK-028 确认 CHK-028：必要时可在 10 分钟内回滚。
- [ ] CHK-029 确认 CHK-029：用户公告文案已准备。
- [ ] CHK-030 确认 CHK-030：发布后的复盘模板已准备。
- [ ] CHK-031 确认 CHK-031：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-032 确认 CHK-032：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-033 确认 CHK-033：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-034 确认 CHK-034：回滚条件与回滚负责人已明确。
- [ ] CHK-035 确认 CHK-035：上线窗口与观察窗口已排期。
- [ ] CHK-036 确认 CHK-036：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-037 确认 CHK-037：关键配置变更已做备份。
- [ ] CHK-038 确认 CHK-038：新加配置项具备默认值或兼容策略。
- [ ] CHK-039 确认 CHK-039：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-040 确认 CHK-040：发布说明包含 commit、时间、执行人。
- [ ] CHK-041 确认 CHK-041：Python 编译检查已通过。
- [ ] CHK-042 确认 CHK-042：测试套件已执行并记录结果。
- [ ] CHK-043 确认 CHK-043：WebUI 构建已通过。
- [ ] CHK-044 确认 CHK-044：systemd 服务控制命令可用。
- [ ] CHK-045 确认 CHK-045：远程 bootstrap 安装流程已验证。
- [ ] CHK-046 确认 CHK-046：`yukiko --help` 输出符合预期。
- [ ] CHK-047 确认 CHK-047：WebUI 登录、会话、历史接口可用。
- [ ] CHK-048 确认 CHK-048：Thinking 流显示在目标场景可见。
- [ ] CHK-049 确认 CHK-049：下载任务失败时有明确拦截原因。
- [ ] CHK-050 确认 CHK-050：OneBot 重连后队列与会话无异常。
- [ ] CHK-051 确认 CHK-051：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-052 确认 CHK-052：错误日志不会暴露敏感密钥。
- [ ] CHK-053 确认 CHK-053：生产 token 已替换为随机高强度值。
- [ ] CHK-054 确认 CHK-054：CPU/内存占用在可接受范围。
- [ ] CHK-055 确认 CHK-055：异常场景应答路径已演练。
- [ ] CHK-056 确认 CHK-056：发布后监控责任人已就位。
- [ ] CHK-057 确认 CHK-057：值班沟通渠道已确认。
- [ ] CHK-058 确认 CHK-058：必要时可在 10 分钟内回滚。
- [ ] CHK-059 确认 CHK-059：用户公告文案已准备。
- [ ] CHK-060 确认 CHK-060：发布后的复盘模板已准备。

### B. 配置与兼容性

- [ ] CHK-061 确认 CHK-061：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-062 确认 CHK-062：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-063 确认 CHK-063：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-064 确认 CHK-064：回滚条件与回滚负责人已明确。
- [ ] CHK-065 确认 CHK-065：上线窗口与观察窗口已排期。
- [ ] CHK-066 确认 CHK-066：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-067 确认 CHK-067：关键配置变更已做备份。
- [ ] CHK-068 确认 CHK-068：新加配置项具备默认值或兼容策略。
- [ ] CHK-069 确认 CHK-069：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-070 确认 CHK-070：发布说明包含 commit、时间、执行人。
- [ ] CHK-071 确认 CHK-071：Python 编译检查已通过。
- [ ] CHK-072 确认 CHK-072：测试套件已执行并记录结果。
- [ ] CHK-073 确认 CHK-073：WebUI 构建已通过。
- [ ] CHK-074 确认 CHK-074：systemd 服务控制命令可用。
- [ ] CHK-075 确认 CHK-075：远程 bootstrap 安装流程已验证。
- [ ] CHK-076 确认 CHK-076：`yukiko --help` 输出符合预期。
- [ ] CHK-077 确认 CHK-077：WebUI 登录、会话、历史接口可用。
- [ ] CHK-078 确认 CHK-078：Thinking 流显示在目标场景可见。
- [ ] CHK-079 确认 CHK-079：下载任务失败时有明确拦截原因。
- [ ] CHK-080 确认 CHK-080：OneBot 重连后队列与会话无异常。
- [ ] CHK-081 确认 CHK-081：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-082 确认 CHK-082：错误日志不会暴露敏感密钥。
- [ ] CHK-083 确认 CHK-083：生产 token 已替换为随机高强度值。
- [ ] CHK-084 确认 CHK-084：CPU/内存占用在可接受范围。
- [ ] CHK-085 确认 CHK-085：异常场景应答路径已演练。
- [ ] CHK-086 确认 CHK-086：发布后监控责任人已就位。
- [ ] CHK-087 确认 CHK-087：值班沟通渠道已确认。
- [ ] CHK-088 确认 CHK-088：必要时可在 10 分钟内回滚。
- [ ] CHK-089 确认 CHK-089：用户公告文案已准备。
- [ ] CHK-090 确认 CHK-090：发布后的复盘模板已准备。
- [ ] CHK-091 确认 CHK-091：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-092 确认 CHK-092：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-093 确认 CHK-093：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-094 确认 CHK-094：回滚条件与回滚负责人已明确。
- [ ] CHK-095 确认 CHK-095：上线窗口与观察窗口已排期。
- [ ] CHK-096 确认 CHK-096：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-097 确认 CHK-097：关键配置变更已做备份。
- [ ] CHK-098 确认 CHK-098：新加配置项具备默认值或兼容策略。
- [ ] CHK-099 确认 CHK-099：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-100 确认 CHK-100：发布说明包含 commit、时间、执行人。
- [ ] CHK-101 确认 CHK-101：Python 编译检查已通过。
- [ ] CHK-102 确认 CHK-102：测试套件已执行并记录结果。
- [ ] CHK-103 确认 CHK-103：WebUI 构建已通过。
- [ ] CHK-104 确认 CHK-104：systemd 服务控制命令可用。
- [ ] CHK-105 确认 CHK-105：远程 bootstrap 安装流程已验证。
- [ ] CHK-106 确认 CHK-106：`yukiko --help` 输出符合预期。
- [ ] CHK-107 确认 CHK-107：WebUI 登录、会话、历史接口可用。
- [ ] CHK-108 确认 CHK-108：Thinking 流显示在目标场景可见。
- [ ] CHK-109 确认 CHK-109：下载任务失败时有明确拦截原因。
- [ ] CHK-110 确认 CHK-110：OneBot 重连后队列与会话无异常。
- [ ] CHK-111 确认 CHK-111：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-112 确认 CHK-112：错误日志不会暴露敏感密钥。
- [ ] CHK-113 确认 CHK-113：生产 token 已替换为随机高强度值。
- [ ] CHK-114 确认 CHK-114：CPU/内存占用在可接受范围。
- [ ] CHK-115 确认 CHK-115：异常场景应答路径已演练。
- [ ] CHK-116 确认 CHK-116：发布后监控责任人已就位。
- [ ] CHK-117 确认 CHK-117：值班沟通渠道已确认。
- [ ] CHK-118 确认 CHK-118：必要时可在 10 分钟内回滚。
- [ ] CHK-119 确认 CHK-119：用户公告文案已准备。
- [ ] CHK-120 确认 CHK-120：发布后的复盘模板已准备。

### C. 部署与脚本可靠性

- [ ] CHK-121 确认 CHK-121：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-122 确认 CHK-122：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-123 确认 CHK-123：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-124 确认 CHK-124：回滚条件与回滚负责人已明确。
- [ ] CHK-125 确认 CHK-125：上线窗口与观察窗口已排期。
- [ ] CHK-126 确认 CHK-126：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-127 确认 CHK-127：关键配置变更已做备份。
- [ ] CHK-128 确认 CHK-128：新加配置项具备默认值或兼容策略。
- [ ] CHK-129 确认 CHK-129：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-130 确认 CHK-130：发布说明包含 commit、时间、执行人。
- [ ] CHK-131 确认 CHK-131：Python 编译检查已通过。
- [ ] CHK-132 确认 CHK-132：测试套件已执行并记录结果。
- [ ] CHK-133 确认 CHK-133：WebUI 构建已通过。
- [ ] CHK-134 确认 CHK-134：systemd 服务控制命令可用。
- [ ] CHK-135 确认 CHK-135：远程 bootstrap 安装流程已验证。
- [ ] CHK-136 确认 CHK-136：`yukiko --help` 输出符合预期。
- [ ] CHK-137 确认 CHK-137：WebUI 登录、会话、历史接口可用。
- [ ] CHK-138 确认 CHK-138：Thinking 流显示在目标场景可见。
- [ ] CHK-139 确认 CHK-139：下载任务失败时有明确拦截原因。
- [ ] CHK-140 确认 CHK-140：OneBot 重连后队列与会话无异常。
- [ ] CHK-141 确认 CHK-141：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-142 确认 CHK-142：错误日志不会暴露敏感密钥。
- [ ] CHK-143 确认 CHK-143：生产 token 已替换为随机高强度值。
- [ ] CHK-144 确认 CHK-144：CPU/内存占用在可接受范围。
- [ ] CHK-145 确认 CHK-145：异常场景应答路径已演练。
- [ ] CHK-146 确认 CHK-146：发布后监控责任人已就位。
- [ ] CHK-147 确认 CHK-147：值班沟通渠道已确认。
- [ ] CHK-148 确认 CHK-148：必要时可在 10 分钟内回滚。
- [ ] CHK-149 确认 CHK-149：用户公告文案已准备。
- [ ] CHK-150 确认 CHK-150：发布后的复盘模板已准备。
- [ ] CHK-151 确认 CHK-151：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-152 确认 CHK-152：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-153 确认 CHK-153：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-154 确认 CHK-154：回滚条件与回滚负责人已明确。
- [ ] CHK-155 确认 CHK-155：上线窗口与观察窗口已排期。
- [ ] CHK-156 确认 CHK-156：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-157 确认 CHK-157：关键配置变更已做备份。
- [ ] CHK-158 确认 CHK-158：新加配置项具备默认值或兼容策略。
- [ ] CHK-159 确认 CHK-159：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-160 确认 CHK-160：发布说明包含 commit、时间、执行人。
- [ ] CHK-161 确认 CHK-161：Python 编译检查已通过。
- [ ] CHK-162 确认 CHK-162：测试套件已执行并记录结果。
- [ ] CHK-163 确认 CHK-163：WebUI 构建已通过。
- [ ] CHK-164 确认 CHK-164：systemd 服务控制命令可用。
- [ ] CHK-165 确认 CHK-165：远程 bootstrap 安装流程已验证。
- [ ] CHK-166 确认 CHK-166：`yukiko --help` 输出符合预期。
- [ ] CHK-167 确认 CHK-167：WebUI 登录、会话、历史接口可用。
- [ ] CHK-168 确认 CHK-168：Thinking 流显示在目标场景可见。
- [ ] CHK-169 确认 CHK-169：下载任务失败时有明确拦截原因。
- [ ] CHK-170 确认 CHK-170：OneBot 重连后队列与会话无异常。
- [ ] CHK-171 确认 CHK-171：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-172 确认 CHK-172：错误日志不会暴露敏感密钥。
- [ ] CHK-173 确认 CHK-173：生产 token 已替换为随机高强度值。
- [ ] CHK-174 确认 CHK-174：CPU/内存占用在可接受范围。
- [ ] CHK-175 确认 CHK-175：异常场景应答路径已演练。
- [ ] CHK-176 确认 CHK-176：发布后监控责任人已就位。
- [ ] CHK-177 确认 CHK-177：值班沟通渠道已确认。
- [ ] CHK-178 确认 CHK-178：必要时可在 10 分钟内回滚。
- [ ] CHK-179 确认 CHK-179：用户公告文案已准备。
- [ ] CHK-180 确认 CHK-180：发布后的复盘模板已准备。
- [ ] CHK-181 确认 CHK-181：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-182 确认 CHK-182：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-183 确认 CHK-183：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-184 确认 CHK-184：回滚条件与回滚负责人已明确。
- [ ] CHK-185 确认 CHK-185：上线窗口与观察窗口已排期。
- [ ] CHK-186 确认 CHK-186：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-187 确认 CHK-187：关键配置变更已做备份。
- [ ] CHK-188 确认 CHK-188：新加配置项具备默认值或兼容策略。
- [ ] CHK-189 确认 CHK-189：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-190 确认 CHK-190：发布说明包含 commit、时间、执行人。

### D. WebUI 与交互稳定性

- [ ] CHK-191 确认 CHK-191：Python 编译检查已通过。
- [ ] CHK-192 确认 CHK-192：测试套件已执行并记录结果。
- [ ] CHK-193 确认 CHK-193：WebUI 构建已通过。
- [ ] CHK-194 确认 CHK-194：systemd 服务控制命令可用。
- [ ] CHK-195 确认 CHK-195：远程 bootstrap 安装流程已验证。
- [ ] CHK-196 确认 CHK-196：`yukiko --help` 输出符合预期。
- [ ] CHK-197 确认 CHK-197：WebUI 登录、会话、历史接口可用。
- [ ] CHK-198 确认 CHK-198：Thinking 流显示在目标场景可见。
- [ ] CHK-199 确认 CHK-199：下载任务失败时有明确拦截原因。
- [ ] CHK-200 确认 CHK-200：OneBot 重连后队列与会话无异常。
- [ ] CHK-201 确认 CHK-201：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-202 确认 CHK-202：错误日志不会暴露敏感密钥。
- [ ] CHK-203 确认 CHK-203：生产 token 已替换为随机高强度值。
- [ ] CHK-204 确认 CHK-204：CPU/内存占用在可接受范围。
- [ ] CHK-205 确认 CHK-205：异常场景应答路径已演练。
- [ ] CHK-206 确认 CHK-206：发布后监控责任人已就位。
- [ ] CHK-207 确认 CHK-207：值班沟通渠道已确认。
- [ ] CHK-208 确认 CHK-208：必要时可在 10 分钟内回滚。
- [ ] CHK-209 确认 CHK-209：用户公告文案已准备。
- [ ] CHK-210 确认 CHK-210：发布后的复盘模板已准备。
- [ ] CHK-211 确认 CHK-211：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-212 确认 CHK-212：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-213 确认 CHK-213：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-214 确认 CHK-214：回滚条件与回滚负责人已明确。
- [ ] CHK-215 确认 CHK-215：上线窗口与观察窗口已排期。
- [ ] CHK-216 确认 CHK-216：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-217 确认 CHK-217：关键配置变更已做备份。
- [ ] CHK-218 确认 CHK-218：新加配置项具备默认值或兼容策略。
- [ ] CHK-219 确认 CHK-219：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-220 确认 CHK-220：发布说明包含 commit、时间、执行人。
- [ ] CHK-221 确认 CHK-221：Python 编译检查已通过。
- [ ] CHK-222 确认 CHK-222：测试套件已执行并记录结果。
- [ ] CHK-223 确认 CHK-223：WebUI 构建已通过。
- [ ] CHK-224 确认 CHK-224：systemd 服务控制命令可用。
- [ ] CHK-225 确认 CHK-225：远程 bootstrap 安装流程已验证。
- [ ] CHK-226 确认 CHK-226：`yukiko --help` 输出符合预期。
- [ ] CHK-227 确认 CHK-227：WebUI 登录、会话、历史接口可用。
- [ ] CHK-228 确认 CHK-228：Thinking 流显示在目标场景可见。
- [ ] CHK-229 确认 CHK-229：下载任务失败时有明确拦截原因。
- [ ] CHK-230 确认 CHK-230：OneBot 重连后队列与会话无异常。
- [ ] CHK-231 确认 CHK-231：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-232 确认 CHK-232：错误日志不会暴露敏感密钥。
- [ ] CHK-233 确认 CHK-233：生产 token 已替换为随机高强度值。
- [ ] CHK-234 确认 CHK-234：CPU/内存占用在可接受范围。
- [ ] CHK-235 确认 CHK-235：异常场景应答路径已演练。
- [ ] CHK-236 确认 CHK-236：发布后监控责任人已就位。
- [ ] CHK-237 确认 CHK-237：值班沟通渠道已确认。
- [ ] CHK-238 确认 CHK-238：必要时可在 10 分钟内回滚。
- [ ] CHK-239 确认 CHK-239：用户公告文案已准备。
- [ ] CHK-240 确认 CHK-240：发布后的复盘模板已准备。
- [ ] CHK-241 确认 CHK-241：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-242 确认 CHK-242：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-243 确认 CHK-243：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-244 确认 CHK-244：回滚条件与回滚负责人已明确。
- [ ] CHK-245 确认 CHK-245：上线窗口与观察窗口已排期。
- [ ] CHK-246 确认 CHK-246：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-247 确认 CHK-247：关键配置变更已做备份。
- [ ] CHK-248 确认 CHK-248：新加配置项具备默认值或兼容策略。
- [ ] CHK-249 确认 CHK-249：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-250 确认 CHK-250：发布说明包含 commit、时间、执行人。
- [ ] CHK-251 确认 CHK-251：Python 编译检查已通过。
- [ ] CHK-252 确认 CHK-252：测试套件已执行并记录结果。
- [ ] CHK-253 确认 CHK-253：WebUI 构建已通过。
- [ ] CHK-254 确认 CHK-254：systemd 服务控制命令可用。
- [ ] CHK-255 确认 CHK-255：远程 bootstrap 安装流程已验证。
- [ ] CHK-256 确认 CHK-256：`yukiko --help` 输出符合预期。
- [ ] CHK-257 确认 CHK-257：WebUI 登录、会话、历史接口可用。
- [ ] CHK-258 确认 CHK-258：Thinking 流显示在目标场景可见。
- [ ] CHK-259 确认 CHK-259：下载任务失败时有明确拦截原因。
- [ ] CHK-260 确认 CHK-260：OneBot 重连后队列与会话无异常。

### E. Agent 与工具链稳定性

- [ ] CHK-261 确认 CHK-261：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-262 确认 CHK-262：错误日志不会暴露敏感密钥。
- [ ] CHK-263 确认 CHK-263：生产 token 已替换为随机高强度值。
- [ ] CHK-264 确认 CHK-264：CPU/内存占用在可接受范围。
- [ ] CHK-265 确认 CHK-265：异常场景应答路径已演练。
- [ ] CHK-266 确认 CHK-266：发布后监控责任人已就位。
- [ ] CHK-267 确认 CHK-267：值班沟通渠道已确认。
- [ ] CHK-268 确认 CHK-268：必要时可在 10 分钟内回滚。
- [ ] CHK-269 确认 CHK-269：用户公告文案已准备。
- [ ] CHK-270 确认 CHK-270：发布后的复盘模板已准备。
- [ ] CHK-271 确认 CHK-271：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-272 确认 CHK-272：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-273 确认 CHK-273：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-274 确认 CHK-274：回滚条件与回滚负责人已明确。
- [ ] CHK-275 确认 CHK-275：上线窗口与观察窗口已排期。
- [ ] CHK-276 确认 CHK-276：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-277 确认 CHK-277：关键配置变更已做备份。
- [ ] CHK-278 确认 CHK-278：新加配置项具备默认值或兼容策略。
- [ ] CHK-279 确认 CHK-279：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-280 确认 CHK-280：发布说明包含 commit、时间、执行人。
- [ ] CHK-281 确认 CHK-281：Python 编译检查已通过。
- [ ] CHK-282 确认 CHK-282：测试套件已执行并记录结果。
- [ ] CHK-283 确认 CHK-283：WebUI 构建已通过。
- [ ] CHK-284 确认 CHK-284：systemd 服务控制命令可用。
- [ ] CHK-285 确认 CHK-285：远程 bootstrap 安装流程已验证。
- [ ] CHK-286 确认 CHK-286：`yukiko --help` 输出符合预期。
- [ ] CHK-287 确认 CHK-287：WebUI 登录、会话、历史接口可用。
- [ ] CHK-288 确认 CHK-288：Thinking 流显示在目标场景可见。
- [ ] CHK-289 确认 CHK-289：下载任务失败时有明确拦截原因。
- [ ] CHK-290 确认 CHK-290：OneBot 重连后队列与会话无异常。
- [ ] CHK-291 确认 CHK-291：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-292 确认 CHK-292：错误日志不会暴露敏感密钥。
- [ ] CHK-293 确认 CHK-293：生产 token 已替换为随机高强度值。
- [ ] CHK-294 确认 CHK-294：CPU/内存占用在可接受范围。
- [ ] CHK-295 确认 CHK-295：异常场景应答路径已演练。
- [ ] CHK-296 确认 CHK-296：发布后监控责任人已就位。
- [ ] CHK-297 确认 CHK-297：值班沟通渠道已确认。
- [ ] CHK-298 确认 CHK-298：必要时可在 10 分钟内回滚。
- [ ] CHK-299 确认 CHK-299：用户公告文案已准备。
- [ ] CHK-300 确认 CHK-300：发布后的复盘模板已准备。
- [ ] CHK-301 确认 CHK-301：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-302 确认 CHK-302：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-303 确认 CHK-303：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-304 确认 CHK-304：回滚条件与回滚负责人已明确。
- [ ] CHK-305 确认 CHK-305：上线窗口与观察窗口已排期。
- [ ] CHK-306 确认 CHK-306：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-307 确认 CHK-307：关键配置变更已做备份。
- [ ] CHK-308 确认 CHK-308：新加配置项具备默认值或兼容策略。
- [ ] CHK-309 确认 CHK-309：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-310 确认 CHK-310：发布说明包含 commit、时间、执行人。
- [ ] CHK-311 确认 CHK-311：Python 编译检查已通过。
- [ ] CHK-312 确认 CHK-312：测试套件已执行并记录结果。
- [ ] CHK-313 确认 CHK-313：WebUI 构建已通过。
- [ ] CHK-314 确认 CHK-314：systemd 服务控制命令可用。
- [ ] CHK-315 确认 CHK-315：远程 bootstrap 安装流程已验证。
- [ ] CHK-316 确认 CHK-316：`yukiko --help` 输出符合预期。
- [ ] CHK-317 确认 CHK-317：WebUI 登录、会话、历史接口可用。
- [ ] CHK-318 确认 CHK-318：Thinking 流显示在目标场景可见。
- [ ] CHK-319 确认 CHK-319：下载任务失败时有明确拦截原因。
- [ ] CHK-320 确认 CHK-320：OneBot 重连后队列与会话无异常。
- [ ] CHK-321 确认 CHK-321：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-322 确认 CHK-322：错误日志不会暴露敏感密钥。
- [ ] CHK-323 确认 CHK-323：生产 token 已替换为随机高强度值。
- [ ] CHK-324 确认 CHK-324：CPU/内存占用在可接受范围。
- [ ] CHK-325 确认 CHK-325：异常场景应答路径已演练。
- [ ] CHK-326 确认 CHK-326：发布后监控责任人已就位。
- [ ] CHK-327 确认 CHK-327：值班沟通渠道已确认。
- [ ] CHK-328 确认 CHK-328：必要时可在 10 分钟内回滚。
- [ ] CHK-329 确认 CHK-329：用户公告文案已准备。
- [ ] CHK-330 确认 CHK-330：发布后的复盘模板已准备。
- [ ] CHK-331 确认 CHK-331：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-332 确认 CHK-332：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-333 确认 CHK-333：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-334 确认 CHK-334：回滚条件与回滚负责人已明确。
- [ ] CHK-335 确认 CHK-335：上线窗口与观察窗口已排期。
- [ ] CHK-336 确认 CHK-336：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-337 确认 CHK-337：关键配置变更已做备份。
- [ ] CHK-338 确认 CHK-338：新加配置项具备默认值或兼容策略。
- [ ] CHK-339 确认 CHK-339：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-340 确认 CHK-340：发布说明包含 commit、时间、执行人。

### F. 安全与合规

- [ ] CHK-341 确认 CHK-341：Python 编译检查已通过。
- [ ] CHK-342 确认 CHK-342：测试套件已执行并记录结果。
- [ ] CHK-343 确认 CHK-343：WebUI 构建已通过。
- [ ] CHK-344 确认 CHK-344：systemd 服务控制命令可用。
- [ ] CHK-345 确认 CHK-345：远程 bootstrap 安装流程已验证。
- [ ] CHK-346 确认 CHK-346：`yukiko --help` 输出符合预期。
- [ ] CHK-347 确认 CHK-347：WebUI 登录、会话、历史接口可用。
- [ ] CHK-348 确认 CHK-348：Thinking 流显示在目标场景可见。
- [ ] CHK-349 确认 CHK-349：下载任务失败时有明确拦截原因。
- [ ] CHK-350 确认 CHK-350：OneBot 重连后队列与会话无异常。
- [ ] CHK-351 确认 CHK-351：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-352 确认 CHK-352：错误日志不会暴露敏感密钥。
- [ ] CHK-353 确认 CHK-353：生产 token 已替换为随机高强度值。
- [ ] CHK-354 确认 CHK-354：CPU/内存占用在可接受范围。
- [ ] CHK-355 确认 CHK-355：异常场景应答路径已演练。
- [ ] CHK-356 确认 CHK-356：发布后监控责任人已就位。
- [ ] CHK-357 确认 CHK-357：值班沟通渠道已确认。
- [ ] CHK-358 确认 CHK-358：必要时可在 10 分钟内回滚。
- [ ] CHK-359 确认 CHK-359：用户公告文案已准备。
- [ ] CHK-360 确认 CHK-360：发布后的复盘模板已准备。
- [ ] CHK-361 确认 CHK-361：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-362 确认 CHK-362：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-363 确认 CHK-363：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-364 确认 CHK-364：回滚条件与回滚负责人已明确。
- [ ] CHK-365 确认 CHK-365：上线窗口与观察窗口已排期。
- [ ] CHK-366 确认 CHK-366：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-367 确认 CHK-367：关键配置变更已做备份。
- [ ] CHK-368 确认 CHK-368：新加配置项具备默认值或兼容策略。
- [ ] CHK-369 确认 CHK-369：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-370 确认 CHK-370：发布说明包含 commit、时间、执行人。
- [ ] CHK-371 确认 CHK-371：Python 编译检查已通过。
- [ ] CHK-372 确认 CHK-372：测试套件已执行并记录结果。
- [ ] CHK-373 确认 CHK-373：WebUI 构建已通过。
- [ ] CHK-374 确认 CHK-374：systemd 服务控制命令可用。
- [ ] CHK-375 确认 CHK-375：远程 bootstrap 安装流程已验证。
- [ ] CHK-376 确认 CHK-376：`yukiko --help` 输出符合预期。
- [ ] CHK-377 确认 CHK-377：WebUI 登录、会话、历史接口可用。
- [ ] CHK-378 确认 CHK-378：Thinking 流显示在目标场景可见。
- [ ] CHK-379 确认 CHK-379：下载任务失败时有明确拦截原因。
- [ ] CHK-380 确认 CHK-380：OneBot 重连后队列与会话无异常。
- [ ] CHK-381 确认 CHK-381：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-382 确认 CHK-382：错误日志不会暴露敏感密钥。
- [ ] CHK-383 确认 CHK-383：生产 token 已替换为随机高强度值。
- [ ] CHK-384 确认 CHK-384：CPU/内存占用在可接受范围。
- [ ] CHK-385 确认 CHK-385：异常场景应答路径已演练。
- [ ] CHK-386 确认 CHK-386：发布后监控责任人已就位。
- [ ] CHK-387 确认 CHK-387：值班沟通渠道已确认。
- [ ] CHK-388 确认 CHK-388：必要时可在 10 分钟内回滚。
- [ ] CHK-389 确认 CHK-389：用户公告文案已准备。
- [ ] CHK-390 确认 CHK-390：发布后的复盘模板已准备。
- [ ] CHK-391 确认 CHK-391：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-392 确认 CHK-392：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-393 确认 CHK-393：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-394 确认 CHK-394：回滚条件与回滚负责人已明确。
- [ ] CHK-395 确认 CHK-395：上线窗口与观察窗口已排期。
- [ ] CHK-396 确认 CHK-396：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-397 确认 CHK-397：关键配置变更已做备份。
- [ ] CHK-398 确认 CHK-398：新加配置项具备默认值或兼容策略。
- [ ] CHK-399 确认 CHK-399：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-400 确认 CHK-400：发布说明包含 commit、时间、执行人。

### G. 观测与可恢复性

- [ ] CHK-401 确认 CHK-401：Python 编译检查已通过。
- [ ] CHK-402 确认 CHK-402：测试套件已执行并记录结果。
- [ ] CHK-403 确认 CHK-403：WebUI 构建已通过。
- [ ] CHK-404 确认 CHK-404：systemd 服务控制命令可用。
- [ ] CHK-405 确认 CHK-405：远程 bootstrap 安装流程已验证。
- [ ] CHK-406 确认 CHK-406：`yukiko --help` 输出符合预期。
- [ ] CHK-407 确认 CHK-407：WebUI 登录、会话、历史接口可用。
- [ ] CHK-408 确认 CHK-408：Thinking 流显示在目标场景可见。
- [ ] CHK-409 确认 CHK-409：下载任务失败时有明确拦截原因。
- [ ] CHK-410 确认 CHK-410：OneBot 重连后队列与会话无异常。
- [ ] CHK-411 确认 CHK-411：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-412 确认 CHK-412：错误日志不会暴露敏感密钥。
- [ ] CHK-413 确认 CHK-413：生产 token 已替换为随机高强度值。
- [ ] CHK-414 确认 CHK-414：CPU/内存占用在可接受范围。
- [ ] CHK-415 确认 CHK-415：异常场景应答路径已演练。
- [ ] CHK-416 确认 CHK-416：发布后监控责任人已就位。
- [ ] CHK-417 确认 CHK-417：值班沟通渠道已确认。
- [ ] CHK-418 确认 CHK-418：必要时可在 10 分钟内回滚。
- [ ] CHK-419 确认 CHK-419：用户公告文案已准备。
- [ ] CHK-420 确认 CHK-420：发布后的复盘模板已准备。
- [ ] CHK-421 确认 CHK-421：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-422 确认 CHK-422：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-423 确认 CHK-423：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-424 确认 CHK-424：回滚条件与回滚负责人已明确。
- [ ] CHK-425 确认 CHK-425：上线窗口与观察窗口已排期。
- [ ] CHK-426 确认 CHK-426：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-427 确认 CHK-427：关键配置变更已做备份。
- [ ] CHK-428 确认 CHK-428：新加配置项具备默认值或兼容策略。
- [ ] CHK-429 确认 CHK-429：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-430 确认 CHK-430：发布说明包含 commit、时间、执行人。
- [ ] CHK-431 确认 CHK-431：Python 编译检查已通过。
- [ ] CHK-432 确认 CHK-432：测试套件已执行并记录结果。
- [ ] CHK-433 确认 CHK-433：WebUI 构建已通过。
- [ ] CHK-434 确认 CHK-434：systemd 服务控制命令可用。
- [ ] CHK-435 确认 CHK-435：远程 bootstrap 安装流程已验证。
- [ ] CHK-436 确认 CHK-436：`yukiko --help` 输出符合预期。
- [ ] CHK-437 确认 CHK-437：WebUI 登录、会话、历史接口可用。
- [ ] CHK-438 确认 CHK-438：Thinking 流显示在目标场景可见。
- [ ] CHK-439 确认 CHK-439：下载任务失败时有明确拦截原因。
- [ ] CHK-440 确认 CHK-440：OneBot 重连后队列与会话无异常。
- [ ] CHK-441 确认 CHK-441：日志关键字段完整（trace、conversation、step）。
- [ ] CHK-442 确认 CHK-442：错误日志不会暴露敏感密钥。
- [ ] CHK-443 确认 CHK-443：生产 token 已替换为随机高强度值。
- [ ] CHK-444 确认 CHK-444：CPU/内存占用在可接受范围。
- [ ] CHK-445 确认 CHK-445：异常场景应答路径已演练。
- [ ] CHK-446 确认 CHK-446：发布后监控责任人已就位。
- [ ] CHK-447 确认 CHK-447：值班沟通渠道已确认。
- [ ] CHK-448 确认 CHK-448：必要时可在 10 分钟内回滚。
- [ ] CHK-449 确认 CHK-449：用户公告文案已准备。
- [ ] CHK-450 确认 CHK-450：发布后的复盘模板已准备。
- [ ] CHK-451 确认 CHK-451：变更背景已写入发布单且可追溯到需求或缺陷。
- [ ] CHK-452 确认 CHK-452：涉及模块边界已标注（后端/前端/脚本/配置）。
- [ ] CHK-453 确认 CHK-453：潜在影响范围（群聊、私聊、下载、插件）已评估。
- [ ] CHK-454 确认 CHK-454：回滚条件与回滚负责人已明确。
- [ ] CHK-455 确认 CHK-455：上线窗口与观察窗口已排期。
- [ ] CHK-456 确认 CHK-456：本地 `git status` 输出已审阅，无误提交。
- [ ] CHK-457 确认 CHK-457：关键配置变更已做备份。
- [ ] CHK-458 确认 CHK-458：新加配置项具备默认值或兼容策略。
- [ ] CHK-459 确认 CHK-459：文档已覆盖使用方式、限制、故障处理。
- [ ] CHK-460 确认 CHK-460：发布说明包含 commit、时间、执行人。

---

## 19. 附录 A：日志字段词典（排障必备）

| 字段 | 说明 | 示例 |
| --- | --- | --- |
| trace_001 | 一次 Agent 执行链唯一追踪标识 | 666451-2-3c72517d |
| step_002 | 工具调用步序号 | step=5 |
| tool_003 | 调用工具名 | smart_download |
| ok_004 | 工具是否成功 | ok=False |
| conversation_005 | 会话键 | group:***REMOVED***:user:***REMOVED*** |
| seq_006 | 队列序列号 | seq=2 |
| reason_007 | 结束原因 | agent_fallback_max_steps_reached |
| group_id_008 | 目标群号 | ***REMOVED*** |
| user_id_009 | 用户编号 | ***REMOVED*** |
| bot_010 | 机器人账号 | ***REMOVED*** |
| post_type_011 | OneBot 事件类型 | message/meta_event |
| message_id_012 | 消息 ID | 1501409008 |
| chunk_count_013 | 分片数量 | chunk_count=2 |
| send_success_014 | 发送成功次数 | send_success=2 |
| status_015 | 系统状态 | online=true |
| interval_016 | 心跳间隔 | 5000 |
| timeout_017 | 超时预算 | outer=246.0s |
| pending_018 | 队列剩余 | pending=0 |
| delivered_019 | 是否投递成功 | delivered=True |
| rate_limited_020 | 是否触发限流 | rate_limited=False |
| trace_021 | 一次 Agent 执行链唯一追踪标识 | 666451-2-3c72517d |
| step_022 | 工具调用步序号 | step=5 |
| tool_023 | 调用工具名 | smart_download |
| ok_024 | 工具是否成功 | ok=False |
| conversation_025 | 会话键 | group:***REMOVED***:user:***REMOVED*** |
| seq_026 | 队列序列号 | seq=2 |
| reason_027 | 结束原因 | agent_fallback_max_steps_reached |
| group_id_028 | 目标群号 | ***REMOVED*** |
| user_id_029 | 用户编号 | ***REMOVED*** |
| bot_030 | 机器人账号 | ***REMOVED*** |
| post_type_031 | OneBot 事件类型 | message/meta_event |
| message_id_032 | 消息 ID | 1501409008 |
| chunk_count_033 | 分片数量 | chunk_count=2 |
| send_success_034 | 发送成功次数 | send_success=2 |
| status_035 | 系统状态 | online=true |
| interval_036 | 心跳间隔 | 5000 |
| timeout_037 | 超时预算 | outer=246.0s |
| pending_038 | 队列剩余 | pending=0 |
| delivered_039 | 是否投递成功 | delivered=True |
| rate_limited_040 | 是否触发限流 | rate_limited=False |
| trace_041 | 一次 Agent 执行链唯一追踪标识 | 666451-2-3c72517d |
| step_042 | 工具调用步序号 | step=5 |
| tool_043 | 调用工具名 | smart_download |
| ok_044 | 工具是否成功 | ok=False |
| conversation_045 | 会话键 | group:***REMOVED***:user:***REMOVED*** |
| seq_046 | 队列序列号 | seq=2 |
| reason_047 | 结束原因 | agent_fallback_max_steps_reached |
| group_id_048 | 目标群号 | ***REMOVED*** |
| user_id_049 | 用户编号 | ***REMOVED*** |
| bot_050 | 机器人账号 | ***REMOVED*** |
| post_type_051 | OneBot 事件类型 | message/meta_event |
| message_id_052 | 消息 ID | 1501409008 |
| chunk_count_053 | 分片数量 | chunk_count=2 |
| send_success_054 | 发送成功次数 | send_success=2 |
| status_055 | 系统状态 | online=true |
| interval_056 | 心跳间隔 | 5000 |
| timeout_057 | 超时预算 | outer=246.0s |
| pending_058 | 队列剩余 | pending=0 |
| delivered_059 | 是否投递成功 | delivered=True |
| rate_limited_060 | 是否触发限流 | rate_limited=False |
| trace_061 | 一次 Agent 执行链唯一追踪标识 | 666451-2-3c72517d |
| step_062 | 工具调用步序号 | step=5 |
| tool_063 | 调用工具名 | smart_download |
| ok_064 | 工具是否成功 | ok=False |
| conversation_065 | 会话键 | group:***REMOVED***:user:***REMOVED*** |
| seq_066 | 队列序列号 | seq=2 |
| reason_067 | 结束原因 | agent_fallback_max_steps_reached |
| group_id_068 | 目标群号 | ***REMOVED*** |
| user_id_069 | 用户编号 | ***REMOVED*** |
| bot_070 | 机器人账号 | ***REMOVED*** |
| post_type_071 | OneBot 事件类型 | message/meta_event |
| message_id_072 | 消息 ID | 1501409008 |
| chunk_count_073 | 分片数量 | chunk_count=2 |
| send_success_074 | 发送成功次数 | send_success=2 |
| status_075 | 系统状态 | online=true |
| interval_076 | 心跳间隔 | 5000 |
| timeout_077 | 超时预算 | outer=246.0s |
| pending_078 | 队列剩余 | pending=0 |
| delivered_079 | 是否投递成功 | delivered=True |
| rate_limited_080 | 是否触发限流 | rate_limited=False |
| trace_081 | 一次 Agent 执行链唯一追踪标识 | 666451-2-3c72517d |
| step_082 | 工具调用步序号 | step=5 |
| tool_083 | 调用工具名 | smart_download |
| ok_084 | 工具是否成功 | ok=False |
| conversation_085 | 会话键 | group:***REMOVED***:user:***REMOVED*** |
| seq_086 | 队列序列号 | seq=2 |
| reason_087 | 结束原因 | agent_fallback_max_steps_reached |
| group_id_088 | 目标群号 | ***REMOVED*** |
| user_id_089 | 用户编号 | ***REMOVED*** |
| bot_090 | 机器人账号 | ***REMOVED*** |
| post_type_091 | OneBot 事件类型 | message/meta_event |
| message_id_092 | 消息 ID | 1501409008 |
| chunk_count_093 | 分片数量 | chunk_count=2 |
| send_success_094 | 发送成功次数 | send_success=2 |
| status_095 | 系统状态 | online=true |
| interval_096 | 心跳间隔 | 5000 |
| timeout_097 | 超时预算 | outer=246.0s |
| pending_098 | 队列剩余 | pending=0 |
| delivered_099 | 是否投递成功 | delivered=True |
| rate_limited_100 | 是否触发限流 | rate_limited=False |
| trace_101 | 一次 Agent 执行链唯一追踪标识 | 666451-2-3c72517d |
| step_102 | 工具调用步序号 | step=5 |
| tool_103 | 调用工具名 | smart_download |
| ok_104 | 工具是否成功 | ok=False |
| conversation_105 | 会话键 | group:***REMOVED***:user:***REMOVED*** |
| seq_106 | 队列序列号 | seq=2 |
| reason_107 | 结束原因 | agent_fallback_max_steps_reached |
| group_id_108 | 目标群号 | ***REMOVED*** |
| user_id_109 | 用户编号 | ***REMOVED*** |
| bot_110 | 机器人账号 | ***REMOVED*** |
| post_type_111 | OneBot 事件类型 | message/meta_event |
| message_id_112 | 消息 ID | 1501409008 |
| chunk_count_113 | 分片数量 | chunk_count=2 |
| send_success_114 | 发送成功次数 | send_success=2 |
| status_115 | 系统状态 | online=true |
| interval_116 | 心跳间隔 | 5000 |
| timeout_117 | 超时预算 | outer=246.0s |
| pending_118 | 队列剩余 | pending=0 |
| delivered_119 | 是否投递成功 | delivered=True |
| rate_limited_120 | 是否触发限流 | rate_limited=False |
| trace_121 | 一次 Agent 执行链唯一追踪标识 | 666451-2-3c72517d |
| step_122 | 工具调用步序号 | step=5 |
| tool_123 | 调用工具名 | smart_download |
| ok_124 | 工具是否成功 | ok=False |
| conversation_125 | 会话键 | group:***REMOVED***:user:***REMOVED*** |
| seq_126 | 队列序列号 | seq=2 |
| reason_127 | 结束原因 | agent_fallback_max_steps_reached |
| group_id_128 | 目标群号 | ***REMOVED*** |
| user_id_129 | 用户编号 | ***REMOVED*** |
| bot_130 | 机器人账号 | ***REMOVED*** |
| post_type_131 | OneBot 事件类型 | message/meta_event |
| message_id_132 | 消息 ID | 1501409008 |
| chunk_count_133 | 分片数量 | chunk_count=2 |
| send_success_134 | 发送成功次数 | send_success=2 |
| status_135 | 系统状态 | online=true |
| interval_136 | 心跳间隔 | 5000 |
| timeout_137 | 超时预算 | outer=246.0s |
| pending_138 | 队列剩余 | pending=0 |
| delivered_139 | 是否投递成功 | delivered=True |
| rate_limited_140 | 是否触发限流 | rate_limited=False |

---

## 20. 附录 B：发布记录模板（可直接复制）

```markdown
# 发布记录

- 发布编号：
- 发布时间：
- 发布执行人：
- 值班人：
- 目标环境：
- 目标版本（commit/tag）：
- 变更范围：
- 风险评级：
- 回滚条件：
- 回滚步骤：

## 自检结果

- Python 编译：
- Pytest：
- WebUI 构建：
- 脚本 help 检查：
- 冒烟用例：

## 观察结果（发布后 30 分钟）

- 错误率：
- 超时率：
- 工具失败热点：
- 用户反馈：

## 结论

- 是否稳定：
- 是否需要补丁：
- 是否触发回滚：
```

---

## 21. 结语

- 发布不是“保证永不出错”，而是“即使出错也能快速恢复”。
- 你可以把这份文档当成发布演练脚本，用事实代替感觉。
- 只要每次发布都按清单执行，稳定性会持续提升。

