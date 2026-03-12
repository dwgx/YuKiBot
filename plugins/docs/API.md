# SelfLearning 插件 API 文档

## 目录

- [Agent 工具 API](#agent-工具-api)
- [插件类 API](#插件类-api)
- [数据模型](#数据模型)
- [配置 API](#配置-api)
- [错误处理](#错误处理)

## Agent 工具 API

### learn_from_web

从网上学习新知识或技术。

**工具名称:** `learn_from_web`

**描述:** Agent 可以搜索、阅读文档、学习新的编程技巧。学习完成后会生成学习报告和可能的代码实现。

**参数:**

| 参数名 | 类型 | 必需 | 描述 |
|--------|------|------|------|
| topic | string | 是 | 要学习的主题或技术 |
| goal | string | 是 | 学习目标，想要实现什么功能 |
| context | string | 否 | 额外的上下文信息 |

**返回值:**

```typescript
{
  ok: boolean,
  data: {
    session_id: string,
    topic: string,
    learned_content?: string
  },
  display: string
}
```

**示例:**

```json
{
  "tool": "learn_from_web",
  "args": {
    "topic": "Python 异步编程",
    "goal": "学会使用 asyncio 编写异步代码",
    "context": "需要处理大量并发请求"
  }
}
```

**响应:**

```json
{
  "ok": true,
  "data": {
    "session_id": "learn_1710234567",
    "topic": "Python 异步编程"
  },
  "display": "已开始学习 'Python 异步编程'...\n\n建议步骤:\n1. 使用 web_search 搜索相关文档\n2. 阅读 asyncio 官方文档\n..."
}
```

**错误情况:**

| 错误 | 原因 | 解决方法 |
|------|------|---------|
| `ok: false` | 缺少必需参数 | 提供 topic 和 goal |
| `ok: false` | 学习超时 | 减少学习范围或增加超时时间 |

---

### create_skill

创建一个新的技能工具。

**工具名称:** `create_skill`

**描述:** Agent 可以编写代码实现新功能，并将其保存为可重用的技能。

**参数:**

| 参数名 | 类型 | 必需 | 描述 |
|--------|------|------|------|
| skill_name | string | 是 | 技能名称（英文，下划线分隔，如 `json_parser`） |
| description | string | 是 | 技能描述 |
| code | string | 是 | 技能的 Python 代码实现 |
| test_code | string | 否 | 测试代码 |

**命名规则:**

- 必须以小写字母开头
- 只能包含小写字母、数字和下划线
- 不能使用 Python 保留字
- 建议使用描述性名称

**代码要求:**

- 必须是有效的 Python 代码
- 不超过配置的最大行数（默认 500 行）
- 避免使用危险操作（如 `eval`, `exec`, `os.system`）
- 建议包含文档字符串

**返回值:**

```typescript
{
  ok: boolean,
  data: {
    skill_name: string,
    saved: boolean,
    test_passed?: boolean,
    code_hash?: string
  },
  display: string,
  error?: string
}
```

**示例:**

```json
{
  "tool": "create_skill",
  "args": {
    "skill_name": "url_validator",
    "description": "验证 URL 格式是否正确",
    "code": "import re\n\ndef validate_url(url: str) -> bool:\n    pattern = r'^https?://[\\w\\-\\.]+(:\\d+)?(/.*)?$'\n    return bool(re.match(pattern, url))\n\nif __name__ == '__main__':\n    assert validate_url('https://example.com')\n    assert not validate_url('invalid')\n    print('测试通过')"
  }
}
```

**响应（成功）:**

```json
{
  "ok": true,
  "data": {
    "skill_name": "url_validator",
    "saved": true,
    "test_passed": true,
    "code_hash": "a1b2c3d4e5f6g7h8"
  },
  "display": "技能 'url_validator' 创建成功！\n描述: 验证 URL 格式是否正确\n代码行数: 12\n测试: 通过 ✓"
}
```

**响应（失败）:**

```json
{
  "ok": false,
  "data": {
    "test_failed": true,
    "error": "NameError: name 'invalid_function' is not defined"
  },
  "display": "代码测试失败: NameError: name 'invalid_function' is not defined"
}
```

**错误情况:**

| 错误 | 原因 | 解决方法 |
|------|------|---------|
| 技能名称格式错误 | 不符合命名规则 | 使用小写字母和下划线 |
| 代码行数超限 | 超过 max_code_lines | 简化代码或拆分成多个技能 |
| 测试失败 | 代码有错误 | 修复代码后重试 |
| 重复技能 | 相同代码已存在 | 使用现有技能或修改代码 |

---

### test_in_sandbox

在隔离的沙盒环境中测试代码。

**工具名称:** `test_in_sandbox`

**描述:** 可以安全地运行和验证代码，不会影响主系统。

**参数:**

| 参数名 | 类型 | 必需 | 描述 |
|--------|------|------|------|
| code | string | 是 | 要测试的 Python 代码 |
| test_input | string | 否 | 测试输入数据 |

**沙盒限制:**

根据配置的沙盒模式，有不同的限制：

| 限制项 | isolated | restricted | full |
|--------|----------|------------|------|
| 环境变量 | 最小化 | 部分 | 完整 |
| 文件系统 | 只读 | 受限写入 | 完整 |
| 网络访问 | 禁用 | 受限 | 完整 |
| 执行时间 | 限制 | 限制 | 限制 |

**返回值:**

```typescript
{
  ok: boolean,
  data: {
    output?: string,
    error?: string,
    returncode?: number,
    execution_time?: number
  },
  display: string
}
```

**示例:**

```json
{
  "tool": "test_in_sandbox",
  "args": {
    "code": "def fibonacci(n):\n    if n <= 1:\n        return n\n    return fibonacci(n-1) + fibonacci(n-2)\n\nprint(fibonacci(10))"
  }
}
```

**响应（成功）:**

```json
{
  "ok": true,
  "data": {
    "output": "55\n",
    "returncode": 0,
    "execution_time": 0.023
  },
  "display": "测试通过 ✓\n输出:\n55"
}
```

**响应（失败）:**

```json
{
  "ok": false,
  "data": {
    "error": "RecursionError: maximum recursion depth exceeded",
    "returncode": 1,
    "execution_time": 1.234
  },
  "display": "测试失败 ✗\n错误:\nRecursionError: maximum recursion depth exceeded"
}
```

**错误情况:**

| 错误 | 原因 | 解决方法 |
|------|------|---------|
| 超时 | 执行时间过长 | 优化算法或增加超时时间 |
| 语法错误 | 代码有语法问题 | 修复语法错误 |
| 运行时错误 | 代码逻辑错误 | 调试并修复代码 |
| 权限错误 | 尝试访问受限资源 | 调整代码或沙盒模式 |

---

### send_devlog

在群里发送开发日志（DEVLOG）。

**工具名称:** `send_devlog`

**描述:** 用白话文告诉大家你学到了什么、做了什么改进。让用户了解你的学习和成长过程。

**参数:**

| 参数名 | 类型 | 必需 | 描述 |
|--------|------|------|------|
| message | string | 是 | 日志内容（白话文） |
| log_type | string | 否 | 日志类型：learning/coding/testing/success/failed |

**日志类型:**

| 类型 | 图标 | 使用场景 |
|------|------|---------|
| learning | 📚 | 开始学习新知识 |
| coding | 💻 | 正在编写代码 |
| testing | 🧪 | 正在测试代码 |
| success | ✅ | 成功完成任务 |
| failed | ❌ | 遇到失败 |

**白话文指南:**

✅ **好的例子:**
- "我刚学会了怎么处理 JSON 数据！"
- "正在写一个图片压缩工具，马上就好~"
- "测试通过了！现在可以帮你们处理 Excel 文件啦"

❌ **不好的例子:**
- "Successfully implemented JSON parser using json.loads()"
- "Executing unit tests for image compression module"
- "Skill creation completed with 0 errors"

**返回值:**

```typescript
{
  ok: boolean,
  data: {
    message: string,
    type: string,
    sent_to?: string
  },
  display: string
}
```

**示例:**

```json
{
  "tool": "send_devlog",
  "args": {
    "message": "我刚学会了异步编程！以后可以同时处理很多任务了~",
    "log_type": "success"
  }
}
```

**响应:**

```json
{
  "ok": true,
  "data": {
    "message": "✅ DEVLOG | 我刚学会了异步编程！以后可以同时处理很多任务了~",
    "type": "success",
    "sent_to": "group_901738883"
  },
  "display": "DEVLOG 已发送: ✅ DEVLOG | 我刚学会了异步编程！以后可以同时处理很多任务了~"
}
```

**错误情况:**

| 错误 | 原因 | 解决方法 |
|------|------|---------|
| 冷却中 | 发送太频繁 | 等待冷却时间结束 |
| 无权限 | 没有群消息权限 | 检查 bot 权限配置 |
| 广播禁用 | devlog_broadcast=false | 启用广播功能 |

---

### list_my_skills

列出所有已创建的技能。

**工具名称:** `list_my_skills`

**描述:** 查看自己学会了什么技能。

**参数:** 无

**返回值:**

```typescript
{
  ok: boolean,
  data: {
    skills: Array<{
      name: string,
      description: string,
      created_at: string,
      test_passed?: boolean
    }>,
    count: number
  },
  display: string
}
```

**示例:**

```json
{
  "tool": "list_my_skills",
  "args": {}
}
```

**响应:**

```json
{
  "ok": true,
  "data": {
    "skills": [
      {
        "name": "json_parser",
        "description": "JSON 数据解析和格式化工具",
        "created_at": "2026-03-12T10:30:00",
        "test_passed": true
      },
      {
        "name": "url_validator",
        "description": "验证 URL 格式是否正确",
        "created_at": "2026-03-12T11:15:00",
        "test_passed": true
      }
    ],
    "count": 2
  },
  "display": "已创建的技能:\n\n1. json_parser\n   描述: JSON 数据解析和格式化工具\n   创建时间: 2026-03-12T10:30:00\n\n2. url_validator\n   描述: 验证 URL 格式是否正确\n   创建时间: 2026-03-12T11:15:00"
}
```

---

## 插件类 API

### Plugin

主插件类。

#### 类属性

```python
class Plugin:
    name: str = "self_learning"
    description: str = "Agent 自我学习系统"
    agent_tool: bool = True
    internal_only: bool = False
```

#### 方法

##### `needs_setup() -> bool`

检查是否需要首次配置。

**返回:** 如果配置文件不存在返回 `True`

##### `interactive_setup() -> dict[str, Any]`

交互式配置向导。

**返回:** 配置字典

##### `async setup(config: dict, context: Any) -> None`

初始化插件。

**参数:**
- `config`: 配置字典
- `context`: 上下文对象（包含 agent_tool_registry）

##### `async teardown() -> None`

清理资源。

##### `get_stats() -> dict[str, Any]`

获取统计信息。

**返回:**
```python
{
    "total_sessions": int,
    "successful_skills": int,
    "failed_skills": int,
    "total_tests": int,
    "passed_tests": int,
    "devlogs_sent": int,
    "active_sessions": int,
    "cached_skills": int,
    "success_rate": float
}
```

---

## 数据模型

### LearningSession

学习会话数据类。

```python
@dataclass
class LearningSession:
    session_id: str
    topic: str
    start_time: datetime
    end_time: datetime | None = None
    status: str = "learning"
    learned_content: str = ""
    code_written: str = ""
    test_results: list[str] = field(default_factory=list)
    devlog: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
```

**方法:**

- `duration() -> float`: 计算会话持续时间（秒）
- `to_dict() -> dict`: 转换为字典格式

**状态值:**

- `learning`: 正在学习
- `coding`: 正在编写代码
- `testing`: 正在测试
- `completed`: 已完成
- `failed`: 失败

### SkillMetadata

技能元数据（YAML 格式）。

```yaml
name: skill_name
description: 技能描述
created_at: '2026-03-12T10:30:00'
test_passed: true
code_hash: a1b2c3d4e5f6g7h8
tags:
  - tag1
  - tag2
dependencies: []
performance:
  avg_execution_time_ms: 1.5
  memory_usage_kb: 100
```

---

## 配置 API

### 配置文件

位置: `plugins/config/self_learning.yml`

```yaml
# 基础配置
enabled: boolean
sandbox_mode: "isolated" | "restricted" | "full"
auto_test: boolean
devlog_broadcast: boolean
learning_source: "web" | "docs" | "both"
save_skills: boolean

# 限制配置
max_learning_time_seconds: number
max_code_lines: number
test_timeout_seconds: number
devlog_cooldown_seconds: number
```

### 环境变量

可以通过环境变量覆盖配置：

```bash
YUKIKO_SELF_LEARNING_ENABLED=true
YUKIKO_SELF_LEARNING_SANDBOX_MODE=isolated
YUKIKO_SELF_LEARNING_AUTO_TEST=true
YUKIKO_SELF_LEARNING_DEVLOG_BROADCAST=true
```

---

## 错误处理

### 错误类型

#### ValidationError

参数验证失败。

```python
{
    "ok": false,
    "error": "validation_error",
    "display": "技能名称必须是小写字母开头，只能包含字母、数字和下划线"
}
```

#### TimeoutError

执行超时。

```python
{
    "ok": false,
    "error": "timeout",
    "display": "测试超时 (60秒)"
}
```

#### SandboxError

沙盒执行错误。

```python
{
    "ok": false,
    "error": "sandbox_error",
    "display": "沙盒执行失败: PermissionError"
}
```

#### DuplicateSkillError

重复的技能。

```python
{
    "ok": false,
    "error": "duplicate_skill",
    "display": "技能 'json_parser' 已存在"
}
```

### 错误处理最佳实践

```python
# 调用 Agent 工具
result = await agent.call_tool("create_skill", {
    "skill_name": "my_skill",
    "description": "...",
    "code": "..."
})

# 检查结果
if result.ok:
    # 成功
    skill_name = result.data["skill_name"]
    print(f"技能 {skill_name} 创建成功")
else:
    # 失败
    error = result.error or "unknown_error"
    print(f"创建失败: {error}")

    # 根据错误类型处理
    if "validation_error" in error:
        # 参数错误，修正参数后重试
        pass
    elif "timeout" in error:
        # 超时，简化代码或增加超时时间
        pass
    elif "duplicate_skill" in error:
        # 重复，使用现有技能
        pass
```

---

## 使用示例

### 完整工作流

```python
# 1. 学习新知识
learn_result = await agent.call_tool("learn_from_web", {
    "topic": "Python 数据验证",
    "goal": "学会使用 pydantic 验证数据",
    "context": "需要验证 API 输入"
})

if learn_result.ok:
    session_id = learn_result.data["session_id"]

    # 2. 发送学习日志
    await agent.call_tool("send_devlog", {
        "message": "我正在学习数据验证，pydantic 看起来很强大！",
        "log_type": "learning"
    })

    # 3. 创建技能
    skill_result = await agent.call_tool("create_skill", {
        "skill_name": "data_validator",
        "description": "使用 pydantic 验证数据",
        "code": """
from pydantic import BaseModel, validator

class UserData(BaseModel):
    name: str
    age: int

    @validator('age')
    def age_must_be_positive(cls, v):
        if v < 0:
            raise ValueError('年龄必须是正数')
        return v

def validate_user(data: dict) -> bool:
    try:
        UserData(**data)
        return True
    except Exception:
        return False
"""
    })

    if skill_result.ok:
        # 4. 发送成功日志
        await agent.call_tool("send_devlog", {
            "message": "学会了数据验证！现在可以帮你验证 API 输入了~",
            "log_type": "success"
        })

        # 5. 查看所有技能
        skills_result = await agent.call_tool("list_my_skills", {})
        print(skills_result.display)
```

---

## 版本历史

### v1.0.0 (2026-03-12)

初始版本，包含：
- 5 个 Agent 工具
- 沙盒执行环境
- DEVLOG 系统
- 技能管理
- 自动测试

---

## 支持

- 文档: [README](../SELF_LEARNING_README.md)
- 示例: [EXAMPLES](../SELF_LEARNING_EXAMPLES.py)
- 架构: [ARCHITECTURE](ARCHITECTURE.md)
- Issues: [GitHub Issues](https://github.com/your-repo/issues)
