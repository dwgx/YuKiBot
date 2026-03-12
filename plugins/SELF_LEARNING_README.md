# SelfLearning 插件 - Agent 自我学习系统

## 功能概述

这个插件让 YuKiKo Agent 能够：

1. **自主学习** - 从网上搜索和学习新知识、新技术
2. **编写代码** - 根据学习内容自己写代码实现新功能
3. **创建技能** - 将学到的能力保存为可重用的 SKILL 工具
4. **沙盒测试** - 在隔离环境中安全测试代码
5. **发送日志** - 用白话文在群里分享学习进展（DEVLOG）

## 工作流程

```
用户提出需求
    ↓
Agent 发现自己不会
    ↓
learn_from_web (搜索学习)
    ↓
create_skill (编写代码)
    ↓
test_in_sandbox (测试代码)
    ↓
send_devlog (告诉用户)
    ↓
技能保存，下次可用
```

## Agent 工具

### 1. learn_from_web
从网上学习新知识或技术。

**参数:**
- `topic` (必需): 要学习的主题
- `goal` (必需): 学习目标
- `context` (可选): 额外的上下文信息

**示例:**
```json
{
  "tool": "learn_from_web",
  "args": {
    "topic": "Python 图片压缩",
    "goal": "学会用 Pillow 库压缩图片",
    "context": "用户想要批量压缩图片"
  }
}
```

### 2. create_skill
创建一个新的技能工具。

**参数:**
- `skill_name` (必需): 技能名称（小写字母+下划线）
- `description` (必需): 技能描述
- `code` (必需): Python 代码实现
- `test_code` (可选): 测试代码

**示例:**
```json
{
  "tool": "create_skill",
  "args": {
    "skill_name": "compress_image",
    "description": "压缩图片文件大小",
    "code": "from PIL import Image\n\ndef compress(path, quality=85):\n    img = Image.open(path)\n    img.save(path, optimize=True, quality=quality)\n    return True"
  }
}
```

### 3. test_in_sandbox
在沙盒环境中测试代码。

**参数:**
- `code` (必需): 要测试的代码
- `test_input` (可选): 测试输入

**示例:**
```json
{
  "tool": "test_in_sandbox",
  "args": {
    "code": "print('Hello, World!')\nprint(2 + 2)"
  }
}
```

### 4. send_devlog
发送开发日志到群。

**参数:**
- `message` (必需): 日志内容（白话文）
- `log_type` (可选): 日志类型 (learning/coding/testing/success/failed)

**示例:**
```json
{
  "tool": "send_devlog",
  "args": {
    "message": "我刚学会了怎么压缩图片！以后可以帮你们处理大图片啦~",
    "log_type": "success"
  }
}
```

### 5. list_my_skills
列出所有已创建的技能。

**示例:**
```json
{
  "tool": "list_my_skills",
  "args": {}
}
```

## DEVLOG 示例

Agent 会用白话文发送学习日志，例如：

```
📚 DEVLOG | 我正在学习怎么解析 JSON 数据，看起来不难！

💻 DEVLOG | 写好了一个 JSON 解析器，正在测试...

🧪 DEVLOG | 测试中... 嗯，有个小 bug，修复一下

✅ DEVLOG | 搞定了！现在我可以处理 API 返回的 JSON 数据了~

📝 DEVLOG | 今天学会了 3 个新技能：JSON 解析、图片压缩、文本分析
```

## 配置说明

### 沙盒模式

- **isolated** (推荐): 完全隔离，最安全
  - 限制环境变量
  - 独立工作目录
  - 无网络访问

- **restricted**: 受限访问
  - 可访问部分系统资源
  - 有限的文件系统访问

- **full**: 完全访问（危险）
  - 仅用于开发和调试
  - 生产环境禁用

### 自动测试

开启后，每次创建技能都会自动在沙盒中测试：
- 语法检查
- 运行时错误检测
- 基本功能验证

### DEVLOG 广播

开启后，Agent 会在群里分享学习进展：
- 学习新知识时
- 编写代码时
- 测试成功/失败时
- 创建新技能时

## 使用场景

### 场景 1: 用户提出新需求

```
用户: "帮我写个工具，能把 PDF 转成图片"

Agent 思考: 我不会 PDF 处理...

Agent 行动:
1. learn_from_web(topic="Python PDF转图片", goal="学会用 pdf2image 库")
2. create_skill(skill_name="pdf_to_images", code="...")
3. test_in_sandbox(code="...")
4. send_devlog("我刚学会了 PDF 转图片！现在可以帮你处理 PDF 了~")
```

### 场景 2: 自我改进

```
Agent 发现: 我的图片处理速度太慢了

Agent 行动:
1. learn_from_web(topic="Python 图片处理优化", goal="提升处理速度")
2. create_skill(skill_name="fast_image_process", code="...")
3. test_in_sandbox(code="...")
4. send_devlog("我优化了图片处理速度，现在快多了！")
```

### 场景 3: 学习新技术

```
Agent 看到: 用户在讨论 WebSocket

Agent 行动:
1. learn_from_web(topic="Python WebSocket", goal="了解实时通信")
2. send_devlog("我在学习 WebSocket，以后可以做实时通信功能了")
3. create_skill(skill_name="websocket_client", code="...")
```

## 技能存储

创建的技能保存在 `storage/self_created_skills/` 目录：

```
storage/self_created_skills/
├── compress_image.py       # 代码实现
├── compress_image.yml      # 元数据
├── pdf_to_images.py
├── pdf_to_images.yml
└── ...
```

## 安全考虑

1. **沙盒隔离**: 所有代码在隔离环境中运行
2. **代码审查**: 可以查看 Agent 创建的所有代码
3. **行数限制**: 防止生成过大的代码文件
4. **超时保护**: 防止无限循环
5. **权限控制**: 可以限制哪些用户能触发学习

## 监控和日志

插件会记录所有学习活动：

```
[INFO] self_learning setup | sandbox=isolated | auto_test=True | devlog=True
[INFO] skill_created | name=compress_image | lines=15
[INFO] devlog_sent | type=success | group=901738883
```

## 故障排除

### 问题: 代码测试总是失败

**解决:**
1. 检查沙盒模式是否太严格
2. 查看测试日志了解具体错误
3. 尝试在 restricted 模式下测试

### 问题: DEVLOG 发送失败

**解决:**
1. 确认 `devlog_broadcast` 已启用
2. 检查 Agent 是否有群消息发送权限
3. 查看冷却时间设置

### 问题: 技能无法保存

**解决:**
1. 检查 `storage/self_created_skills/` 目录权限
2. 确认 `save_skills` 已启用
3. 查看磁盘空间

## 未来扩展

可能的改进方向：

1. **技能市场**: Agent 之间共享学到的技能
2. **版本控制**: 技能的版本管理和回滚
3. **性能分析**: 自动优化代码性能
4. **协作学习**: 多个 Agent 一起学习
5. **知识图谱**: 构建学习知识网络

## 示例对话

```
用户: "你会处理 Excel 文件吗？"

Agent: "让我学习一下... [调用 learn_from_web]"

Agent: "📚 DEVLOG | 我正在学习 Excel 处理，用 openpyxl 库看起来不错！"

Agent: "[创建技能并测试]"

Agent: "✅ DEVLOG | 学会了！现在我可以读写 Excel 文件了~"

Agent: "是的，我现在会处理 Excel 了！需要我帮你做什么？"
```

## 许可和贡献

这个插件是 YuKiKo Bot 项目的一部分。

如果你有改进建议或发现 bug，欢迎提交 Issue 或 PR。
