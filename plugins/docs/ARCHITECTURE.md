# SelfLearning 插件架构文档

## 目录

- [概述](#概述)
- [系统架构](#系统架构)
- [核心组件](#核心组件)
- [数据流](#数据流)
- [安全模型](#安全模型)
- [性能优化](#性能优化)
- [扩展点](#扩展点)

## 概述

SelfLearning 插件是一个让 AI Agent 具备自我学习和自我改进能力的系统。它允许 Agent 在运行时学习新知识、编写新代码、创建新技能，并在沙盒环境中安全测试。

### 设计目标

1. **自主性**: Agent 能够独立学习和创建新功能
2. **安全性**: 所有代码在隔离环境中执行
3. **可追溯**: 完整记录学习过程和决策
4. **可扩展**: 易于添加新的学习源和技能类型
5. **用户友好**: 用白话文与用户交流学习进展

## 系统架构

### 整体架构图

```
┌─────────────────────────────────────────────────────────────┐
│                        YuKiKo Bot                           │
│  ┌───────────────────────────────────────────────────────┐  │
│  │              Agent Core (core/agent.py)               │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │         AgentToolRegistry                       │  │  │
│  │  │  ┌───────────────────────────────────────────┐  │  │  │
│  │  │  │      SelfLearning Plugin                  │  │  │  │
│  │  │  │                                           │  │  │  │
│  │  │  │  ┌─────────────┐  ┌──────────────────┐  │  │  │  │
│  │  │  │  │  Learning   │  │  Skill Manager   │  │  │  │  │
│  │  │  │  │   Engine    │  │                  │  │  │  │  │
│  │  │  │  └──────┬──────┘  └────────┬─────────┘  │  │  │  │
│  │  │  │         │                  │            │  │  │  │
│  │  │  │         ▼                  ▼            │  │  │  │
│  │  │  │  ┌─────────────┐  ┌──────────────────┐  │  │  │  │
│  │  │  │  │   Sandbox   │  │  DEVLOG System   │  │  │  │  │
│  │  │  │  │   Executor  │  │                  │  │  │  │  │
│  │  │  │  └─────────────┘  └──────────────────┘  │  │  │  │
│  │  │  └───────────────────────────────────────────┘  │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │  Storage Layer   │
                    │                  │
                    │  ┌────────────┐  │
                    │  │  Skills DB │  │
                    │  └────────────┘  │
                    │  ┌────────────┐  │
                    │  │  Sessions  │  │
                    │  └────────────┘  │
                    │  ┌────────────┐  │
                    │  │  Sandbox   │  │
                    │  └────────────┘  │
                    └──────────────────┘
```

### 层次结构

```
┌─────────────────────────────────────┐
│      Presentation Layer             │  ← DEVLOG, 用户交互
├─────────────────────────────────────┤
│      Application Layer              │  ← Agent 工具接口
├─────────────────────────────────────┤
│      Business Logic Layer           │  ← 学习引擎、技能管理
├─────────────────────────────────────┤
│      Security Layer                 │  ← 沙盒、权限控制
├─────────────────────────────────────┤
│      Data Layer                     │  ← 技能存储、会话管理
└─────────────────────────────────────┘
```

## 核心组件

### 1. Plugin 类

主插件类，负责整体协调和生命周期管理。

```python
class Plugin:
    """主插件类

    职责:
    - 插件初始化和配置
    - 工具注册
    - 生命周期管理
    - 统计信息收集
    """
```

**关键方法:**
- `setup()`: 初始化插件
- `_register_tools()`: 注册 Agent 工具
- `teardown()`: 清理资源

### 2. LearningSession

学习会话管理器，跟踪单次学习过程。

```python
@dataclass
class LearningSession:
    """学习会话

    生命周期:
    1. 创建 (status=learning)
    2. 编写代码 (status=coding)
    3. 测试 (status=testing)
    4. 完成/失败 (status=completed/failed)
    """
```

**状态转换:**
```
learning → coding → testing → completed
                            ↘ failed
```

### 3. 学习引擎

负责从外部源学习新知识。

**组件:**
- Web 搜索集成
- 文档解析器
- 知识提取器
- 学习策略选择器

**流程:**
```
1. 接收学习主题和目标
2. 选择学习源 (web/docs/both)
3. 搜索和收集资料
4. 提取关键信息
5. 生成学习报告
```

### 4. 技能管理器

管理已创建的技能。

**功能:**
- 技能创建和保存
- 技能缓存
- 重复检测
- 版本管理（未来）

**存储结构:**
```
storage/self_created_skills/
├── skill_name.py          # 代码实现
├── skill_name.yml         # 元数据
└── skill_name.test.py     # 测试代码（可选）
```

### 5. 沙盒执行器

在隔离环境中安全执行代码。

**隔离级别:**

| 模式 | 环境变量 | 文件系统 | 网络 | 适用场景 |
|------|---------|---------|------|---------|
| isolated | 最小化 | 只读 | 禁用 | 生产环境 |
| restricted | 部分 | 受限写入 | 受限 | 测试环境 |
| full | 完整 | 完整 | 完整 | 开发调试 |

**安全措施:**
- 进程隔离
- 超时保护
- 资源限制
- 危险操作检测

### 6. DEVLOG 系统

用白话文向用户报告学习进展。

**日志类型:**
- 📚 learning: 学习新知识
- 💻 coding: 编写代码
- 🧪 testing: 测试代码
- ✅ success: 成功完成
- ❌ failed: 失败

**特点:**
- 白话文表达
- 冷却机制（防刷屏）
- 群消息广播
- 可配置开关

## 数据流

### 学习流程

```
用户请求
    ↓
Agent 识别需求
    ↓
learn_from_web(topic, goal)
    ↓
搜索和学习
    ↓
生成学习报告
    ↓
send_devlog("我正在学习...")
    ↓
create_skill(name, code)
    ↓
代码验证
    ↓
test_in_sandbox(code)
    ↓
测试通过?
    ├─ 是 → 保存技能 → send_devlog("学会了!")
    └─ 否 → 修复代码 → 重新测试
```

### 技能创建流程

```
create_skill 调用
    ↓
参数验证
    ├─ 技能名称格式
    ├─ 代码行数限制
    └─ 必需字段检查
    ↓
重复检测
    ├─ 计算代码哈希
    └─ 查询缓存
    ↓
代码清理
    ├─ 移除危险操作
    └─ 格式化代码
    ↓
自动测试 (如果启用)
    ├─ 创建沙盒环境
    ├─ 执行代码
    └─ 收集结果
    ↓
保存技能 (如果启用)
    ├─ 写入代码文件
    ├─ 写入元数据
    └─ 更新缓存
    ↓
返回结果
```

### 沙盒测试流程

```
test_in_sandbox 调用
    ↓
创建临时文件
    ↓
配置沙盒环境
    ├─ 设置环境变量
    ├─ 设置工作目录
    └─ 配置资源限制
    ↓
启动子进程
    ↓
执行代码 (带超时)
    ├─ 捕获 stdout
    ├─ 捕获 stderr
    └─ 监控执行时间
    ↓
收集结果
    ├─ 返回码
    ├─ 输出内容
    └─ 错误信息
    ↓
清理临时文件
    ↓
更新统计信息
    ↓
返回测试结果
```

## 安全模型

### 威胁模型

**潜在威胁:**
1. 恶意代码注入
2. 资源耗尽攻击
3. 文件系统破坏
4. 网络滥用
5. 权限提升

**防护措施:**

| 威胁 | 防护措施 | 实现位置 |
|------|---------|---------|
| 代码注入 | 代码清理、模式检测 | `_sanitize_code()` |
| 资源耗尽 | 超时、行数限制 | `_test_code_in_sandbox()` |
| 文件破坏 | 沙盒隔离、只读模式 | 环境配置 |
| 网络滥用 | 网络隔离 | isolated 模式 |
| 权限提升 | 最小权限原则 | 环境变量限制 |

### 代码审查

**自动检测的危险模式:**
```python
dangerous_patterns = [
    r"import\s+os\s*\.\s*system",      # 系统命令执行
    r"import\s+subprocess",             # 子进程创建
    r"eval\s*\(",                       # 动态代码执行
    r"exec\s*\(",                       # 动态代码执行
    r"__import__\s*\(",                 # 动态导入
    r"open\s*\([^)]*['\"]w['\"]",      # 文件写入
]
```

### 权限模型

```
┌─────────────────────────────────────┐
│         Super Admin                 │  ← 完全控制
├─────────────────────────────────────┤
│         Group Admin                 │  ← 群内管理
├─────────────────────────────────────┤
│         Regular User                │  ← 基础功能
└─────────────────────────────────────┘
```

## 性能优化

### 缓存策略

**技能缓存:**
- 启动时加载所有技能元数据
- 内存中维护技能索引
- 避免重复的文件 I/O

**会话管理:**
- 限制活跃会话数量
- 自动清理过期会话
- 会话状态持久化（未来）

### 并发控制

**异步执行:**
- 沙盒测试使用 asyncio
- 非阻塞的文件操作
- 并发学习会话支持

**资源限制:**
```python
# 配置示例
max_concurrent_sessions: 5
max_sandbox_processes: 3
session_timeout_seconds: 300
```

### 性能指标

**监控指标:**
- 学习会话数量
- 技能创建成功率
- 测试通过率
- 平均执行时间
- 沙盒资源使用

**统计信息:**
```python
{
    "total_sessions": 42,
    "successful_skills": 38,
    "failed_skills": 4,
    "total_tests": 156,
    "passed_tests": 142,
    "success_rate": 0.91,
    "avg_execution_time": 2.3
}
```

## 扩展点

### 1. 自定义学习源

```python
class CustomLearningSource:
    """自定义学习源接口"""

    async def search(self, topic: str, goal: str) -> str:
        """搜索学习资料"""
        pass

    async def extract_knowledge(self, content: str) -> dict:
        """提取知识点"""
        pass
```

### 2. 技能模板

```python
# 技能模板系统
templates = {
    "data_processor": {
        "imports": ["import json", "import csv"],
        "structure": "class DataProcessor: ...",
    },
    "api_client": {
        "imports": ["import httpx", "import asyncio"],
        "structure": "class APIClient: ...",
    },
}
```

### 3. 测试框架集成

```python
# 集成 pytest
async def run_pytest(skill_path: Path) -> dict:
    """运行 pytest 测试"""
    result = await asyncio.create_subprocess_exec(
        "pytest", str(skill_path), "-v",
        stdout=asyncio.subprocess.PIPE,
    )
    # ...
```

### 4. 技能市场（未来）

```python
class SkillMarketplace:
    """技能市场 - Agent 之间共享技能"""

    async def publish_skill(self, skill: dict) -> str:
        """发布技能到市场"""
        pass

    async def search_skills(self, query: str) -> list[dict]:
        """搜索市场中的技能"""
        pass

    async def install_skill(self, skill_id: str) -> bool:
        """安装市场中的技能"""
        pass
```

### 5. 版本控制

```python
class SkillVersionControl:
    """技能版本控制"""

    def create_version(self, skill_name: str, code: str) -> str:
        """创建新版本"""
        pass

    def rollback(self, skill_name: str, version: str) -> bool:
        """回滚到指定版本"""
        pass

    def diff(self, skill_name: str, v1: str, v2: str) -> str:
        """比较两个版本"""
        pass
```

## 配置管理

### 配置文件结构

```yaml
# plugins/config/self_learning.yml
enabled: true

# 沙盒配置
sandbox:
  mode: isolated
  timeout_seconds: 60
  max_memory_mb: 512
  max_cpu_percent: 50

# 学习配置
learning:
  source: both
  max_time_seconds: 300
  cache_results: true

# 技能配置
skills:
  save_enabled: true
  auto_test: true
  max_code_lines: 500
  version_control: false

# DEVLOG 配置
devlog:
  broadcast: true
  cooldown_seconds: 30
  format: casual  # casual / formal / technical

# 性能配置
performance:
  max_concurrent_sessions: 5
  cache_skills: true
  async_execution: true
```

### 环境变量

```bash
# 覆盖配置
YUKIKO_SELF_LEARNING_ENABLED=true
YUKIKO_SELF_LEARNING_SANDBOX_MODE=isolated
YUKIKO_SELF_LEARNING_AUTO_TEST=true
```

## 监控和日志

### 日志级别

```python
# DEBUG: 详细的调试信息
_log.debug("session_created | id=%s | topic=%s", session_id, topic)

# INFO: 正常操作信息
_log.info("skill_created | name=%s | lines=%d", skill_name, code_lines)

# WARNING: 警告信息
_log.warning("dangerous_code_pattern_detected | pattern=%s", pattern)

# ERROR: 错误信息
_log.error("sandbox_test_failed | error=%s", error)
```

### 性能追踪

```python
# 使用装饰器追踪性能
@track_performance
async def create_skill(...):
    # 自动记录执行时间
    pass
```

### 健康检查

```python
async def health_check() -> dict:
    """插件健康检查"""
    return {
        "status": "healthy",
        "active_sessions": len(self._sessions),
        "sandbox_available": await self._check_sandbox(),
        "skills_count": len(self._skill_cache),
    }
```

## 测试策略

### 单元测试

```python
# 测试技能创建
def test_create_skill_validation():
    # 测试参数验证
    # 测试重复检测
    # 测试代码清理
    pass

# 测试沙盒执行
def test_sandbox_isolation():
    # 测试环境隔离
    # 测试超时保护
    # 测试资源限制
    pass
```

### 集成测试

```python
# 测试完整学习流程
async def test_learning_workflow():
    # 1. 学习
    # 2. 创建技能
    # 3. 测试
    # 4. 保存
    # 5. 验证
    pass
```

### 安全测试

```python
# 测试恶意代码检测
def test_malicious_code_detection():
    # 测试各种危险模式
    pass

# 测试沙盒逃逸
async def test_sandbox_escape_prevention():
    # 尝试各种逃逸方法
    pass
```

## 部署建议

### 生产环境

```yaml
# 推荐配置
sandbox_mode: isolated
auto_test: true
save_skills: true
devlog_broadcast: true
max_code_lines: 200
test_timeout_seconds: 30
```

### 开发环境

```yaml
# 开发配置
sandbox_mode: restricted
auto_test: true
save_skills: true
devlog_broadcast: false
max_code_lines: 1000
test_timeout_seconds: 120
```

### 资源需求

**最小配置:**
- CPU: 2 核
- 内存: 2GB
- 磁盘: 1GB

**推荐配置:**
- CPU: 4 核
- 内存: 4GB
- 磁盘: 5GB

## 未来规划

### 短期 (1-3 个月)

- [ ] 技能版本控制
- [ ] 性能优化
- [ ] 更多学习源
- [ ] 改进测试框架

### 中期 (3-6 个月)

- [ ] 技能市场
- [ ] 协作学习
- [ ] 知识图谱
- [ ] 自动优化

### 长期 (6-12 个月)

- [ ] 分布式学习
- [ ] 联邦学习
- [ ] 元学习能力
- [ ] 自我进化

## 参考资料

- [YuKiKo Bot 架构文档](../../docs/architecture.md)
- [Agent 工具开发指南](../../docs/agent_tools.md)
- [插件开发指南](../../docs/plugin_development.md)
- [安全最佳实践](../../docs/security.md)
