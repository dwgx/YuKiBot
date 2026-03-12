# 贡献指南

感谢你对 SelfLearning 插件的关注！本文档将帮助你了解如何为项目做出贡献。

## 目录

- [行为准则](#行为准则)
- [如何贡献](#如何贡献)
- [开发环境设置](#开发环境设置)
- [代码规范](#代码规范)
- [提交规范](#提交规范)
- [测试要求](#测试要求)
- [文档要求](#文档要求)
- [Pull Request 流程](#pull-request-流程)

## 行为准则

### 我们的承诺

为了营造一个开放和友好的环境，我们承诺：

- 使用友好和包容的语言
- 尊重不同的观点和经验
- 优雅地接受建设性批评
- 关注对社区最有利的事情
- 对其他社区成员表示同理心

### 不可接受的行为

- 使用性化的语言或图像
- 人身攻击或侮辱性评论
- 公开或私下骚扰
- 未经许可发布他人的私人信息
- 其他不道德或不专业的行为

## 如何贡献

### 报告 Bug

发现 bug？请创建一个 Issue，包含：

1. **清晰的标题**: 简洁描述问题
2. **复现步骤**: 详细的步骤说明
3. **预期行为**: 你期望发生什么
4. **实际行为**: 实际发生了什么
5. **环境信息**:
   - YuKiKo 版本
   - Python 版本
   - 操作系统
   - 相关配置

**Bug 报告模板:**

```markdown
## Bug 描述
简要描述 bug

## 复现步骤
1. 执行 '...'
2. 调用工具 '...'
3. 观察错误 '...'

## 预期行为
应该...

## 实际行为
实际上...

## 环境
- YuKiKo 版本: 1.0.0
- Python 版本: 3.11.0
- 操作系统: Windows 11
- 沙盒模式: isolated

## 日志
```
[粘贴相关日志]
```

## 截图
如果适用，添加截图
```

### 建议新功能

有好的想法？创建一个 Feature Request：

1. **功能描述**: 清楚地描述你想要的功能
2. **使用场景**: 为什么需要这个功能
3. **建议实现**: 如果有想法，描述如何实现
4. **替代方案**: 考虑过的其他方案

**功能请求模板:**

```markdown
## 功能描述
我希望能够...

## 使用场景
当我...的时候，我需要...

## 建议实现
可以通过...来实现

## 替代方案
也考虑过...，但是...

## 额外信息
其他相关信息
```

### 改进文档

文档改进同样重要！你可以：

- 修正拼写或语法错误
- 改进现有文档的清晰度
- 添加缺失的文档
- 翻译文档到其他语言
- 添加更多示例

## 开发环境设置

### 1. Fork 和 Clone

```bash
# Fork 项目到你的 GitHub 账号
# 然后 clone 到本地
git clone https://github.com/YOUR_USERNAME/yukiko-bot.git
cd yukiko-bot
```

### 2. 创建虚拟环境

```bash
# 使用 venv
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# 或使用 conda
conda create -n yukiko python=3.11
conda activate yukiko
```

### 3. 安装依赖

```bash
# 安装项目依赖
pip install -r requirements.txt

# 安装开发依赖
pip install -r requirements-dev.txt
```

### 4. 配置插件

```bash
# 复制配置模板
cp plugins/config/self_learning.template.yml plugins/config/self_learning.yml

# 编辑配置
vim plugins/config/self_learning.yml
```

### 5. 运行测试

```bash
# 运行所有测试
python -m pytest tests/

# 运行特定测试
python tests/test_self_learning_plugin.py

# 运行带覆盖率的测试
pytest --cov=plugins.self_learning tests/
```

## 代码规范

### Python 代码风格

我们遵循 [PEP 8](https://pep8.org/) 和项目特定的风格指南。

#### 命名约定

```python
# 类名: PascalCase
class LearningSession:
    pass

# 函数和变量: snake_case
def create_skill(skill_name: str) -> bool:
    max_code_lines = 500
    return True

# 常量: UPPER_SNAKE_CASE
MAX_LEARNING_TIME = 300
_PRIVATE_CONSTANT = "internal"

# 私有方法: 前缀下划线
def _internal_method(self):
    pass
```

#### 类型注解

所有公共 API 必须有类型注解：

```python
# ✅ 好
def create_skill(
    skill_name: str,
    code: str,
    description: str = "",
) -> dict[str, Any]:
    pass

# ❌ 不好
def create_skill(skill_name, code, description=""):
    pass
```

#### 文档字符串

使用 Google 风格的文档字符串：

```python
def test_in_sandbox(code: str, timeout: int = 60) -> dict[str, Any]:
    """在沙盒环境中测试代码。

    Args:
        code: 要测试的 Python 代码
        timeout: 超时时间（秒），默认 60

    Returns:
        包含测试结果的字典:
        - ok: 是否成功
        - output: 标准输出
        - error: 错误信息

    Raises:
        TimeoutError: 执行超时
        SandboxError: 沙盒执行失败

    Example:
        >>> result = test_in_sandbox("print('hello')")
        >>> print(result['output'])
        hello
    """
    pass
```

#### 导入顺序

```python
# 1. 标准库
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

# 2. 第三方库
import yaml
from pydantic import BaseModel

# 3. 本地模块
from core.agent_tools import ToolSchema
from utils.text import clip_text
```

### 代码质量工具

#### Black (代码格式化)

```bash
# 格式化代码
black plugins/self_learning.py

# 检查但不修改
black --check plugins/self_learning.py
```

#### isort (导入排序)

```bash
# 排序导入
isort plugins/self_learning.py

# 检查但不修改
isort --check-only plugins/self_learning.py
```

#### flake8 (代码检查)

```bash
# 检查代码
flake8 plugins/self_learning.py

# 忽略特定错误
flake8 --ignore=E501,W503 plugins/self_learning.py
```

#### mypy (类型检查)

```bash
# 类型检查
mypy plugins/self_learning.py

# 严格模式
mypy --strict plugins/self_learning.py
```

### 配置文件

项目根目录的配置文件：

**`.flake8`:**
```ini
[flake8]
max-line-length = 100
exclude = .git,__pycache__,venv
ignore = E203,W503
```

**`pyproject.toml`:**
```toml
[tool.black]
line-length = 100
target-version = ['py311']

[tool.isort]
profile = "black"
line_length = 100

[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_configs = true
```

## 提交规范

### Commit Message 格式

使用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**类型 (type):**

- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `style`: 代码格式（不影响功能）
- `refactor`: 重构
- `perf`: 性能优化
- `test`: 测试相关
- `chore`: 构建/工具相关

**示例:**

```bash
# 新功能
git commit -m "feat(learning): add support for custom learning sources"

# Bug 修复
git commit -m "fix(sandbox): prevent timeout in isolated mode"

# 文档
git commit -m "docs(api): update create_skill API documentation"

# 重构
git commit -m "refactor(skill): extract skill validation logic"
```

**详细示例:**

```
feat(learning): add support for custom learning sources

- Add CustomLearningSource interface
- Implement plugin system for learning sources
- Add configuration for custom sources
- Update documentation

Closes #123
```

## 测试要求

### 测试覆盖率

- 新功能必须有测试
- 目标覆盖率: 80%+
- 关键路径必须 100% 覆盖

### 测试类型

#### 单元测试

测试单个函数或方法：

```python
def test_skill_name_validation():
    """测试技能名称验证"""
    plugin = Plugin()

    # 有效名称
    assert plugin._validate_skill_name("valid_name")
    assert plugin._validate_skill_name("skill_123")

    # 无效名称
    assert not plugin._validate_skill_name("Invalid-Name")
    assert not plugin._validate_skill_name("123_start")
    assert not plugin._validate_skill_name("has space")
```

#### 集成测试

测试多个组件协作：

```python
async def test_complete_learning_workflow():
    """测试完整学习流程"""
    plugin = Plugin()
    await plugin.setup(test_config, test_context)

    # 1. 学习
    learn_result = await plugin._handle_learn_from_web({
        "topic": "test",
        "goal": "test"
    }, {})
    assert learn_result.ok

    # 2. 创建技能
    skill_result = await plugin._handle_create_skill({
        "skill_name": "test_skill",
        "description": "test",
        "code": "print('test')"
    }, {})
    assert skill_result.ok

    # 3. 验证技能已保存
    skills = await plugin._handle_list_skills({}, {})
    assert "test_skill" in skills.display
```

#### 安全测试

测试安全相关功能：

```python
async def test_dangerous_code_detection():
    """测试危险代码检测"""
    plugin = Plugin()

    dangerous_codes = [
        "import os; os.system('rm -rf /')",
        "eval('malicious_code')",
        "exec('dangerous_operation')",
    ]

    for code in dangerous_codes:
        result = await plugin._test_code_in_sandbox(code)
        # 应该被检测或隔离
        assert not result["ok"] or "denied" in result["error"].lower()
```

### 运行测试

```bash
# 运行所有测试
pytest

# 运行特定文件
pytest tests/test_self_learning_plugin.py

# 运行特定测试
pytest tests/test_self_learning_plugin.py::test_skill_name_validation

# 带覆盖率
pytest --cov=plugins.self_learning --cov-report=html

# 详细输出
pytest -v

# 显示打印输出
pytest -s
```

## 文档要求

### 文档类型

1. **代码文档**: 文档字符串
2. **API 文档**: API.md
3. **架构文档**: ARCHITECTURE.md
4. **用户文档**: README.md
5. **示例**: EXAMPLES.py

### 文档更新

当你修改代码时，确保更新相关文档：

- 新功能 → 更新 README 和 API 文档
- API 变更 → 更新 API 文档
- 架构变更 → 更新 ARCHITECTURE 文档
- 新示例 → 添加到 EXAMPLES

### 文档风格

- 使用清晰、简洁的语言
- 提供代码示例
- 包含使用场景
- 说明限制和注意事项

## Pull Request 流程

### 1. 创建分支

```bash
# 从 main 创建新分支
git checkout -b feature/your-feature-name

# 或修复 bug
git checkout -b fix/bug-description
```

### 2. 开发和测试

```bash
# 编写代码
vim plugins/self_learning.py

# 运行测试
pytest

# 检查代码质量
black plugins/self_learning.py
flake8 plugins/self_learning.py
mypy plugins/self_learning.py
```

### 3. 提交更改

```bash
# 添加文件
git add plugins/self_learning.py

# 提交（遵循提交规范）
git commit -m "feat(learning): add custom learning source support"

# 推送到你的 fork
git push origin feature/your-feature-name
```

### 4. 创建 Pull Request

1. 访问 GitHub 上的项目页面
2. 点击 "New Pull Request"
3. 选择你的分支
4. 填写 PR 描述

**PR 描述模板:**

```markdown
## 变更类型
- [ ] Bug 修复
- [ ] 新功能
- [ ] 重构
- [ ] 文档更新
- [ ] 性能优化

## 变更描述
简要描述你的更改

## 相关 Issue
Closes #123

## 测试
- [ ] 添加了新测试
- [ ] 所有测试通过
- [ ] 手动测试通过

## 检查清单
- [ ] 代码遵循项目规范
- [ ] 添加/更新了文档
- [ ] 添加/更新了测试
- [ ] 通过了所有检查

## 截图
如果适用，添加截图
```

### 5. Code Review

- 响应审查意见
- 进行必要的修改
- 保持讨论专业和友好

### 6. 合并

PR 被批准后，维护者会合并你的代码。

## 开发技巧

### 调试

```python
# 使用日志
import logging
_log = logging.getLogger("yukiko.plugin.self_learning")
_log.debug("Debug message")
_log.info("Info message")
_log.warning("Warning message")
_log.error("Error message")

# 使用 pdb
import pdb; pdb.set_trace()

# 使用 ipdb (更好的 pdb)
import ipdb; ipdb.set_trace()
```

### 性能分析

```python
# 使用 cProfile
python -m cProfile -o output.prof plugins/self_learning.py

# 使用 line_profiler
@profile
def slow_function():
    pass

kernprof -l -v plugins/self_learning.py
```

### 内存分析

```python
# 使用 memory_profiler
from memory_profiler import profile

@profile
def memory_intensive_function():
    pass
```

## 发布流程

### 版本号

遵循 [语义化版本](https://semver.org/)：

- MAJOR: 不兼容的 API 变更
- MINOR: 向后兼容的新功能
- PATCH: 向后兼容的 bug 修复

### 发布步骤

1. 更新版本号
2. 更新 CHANGELOG
3. 创建 Git tag
4. 推送到 GitHub
5. 创建 Release

## 获取帮助

遇到问题？

- 查看 [文档](../SELF_LEARNING_README.md)
- 搜索 [Issues](https://github.com/your-repo/issues)
- 创建新 Issue
- 加入社区讨论

## 致谢

感谢所有贡献者！你们的贡献让这个项目变得更好。

---

**Happy Coding! 🚀**
