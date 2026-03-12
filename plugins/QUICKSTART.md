# SelfLearning 插件 - 快速启动指南

## 1. 安装插件

插件已经创建在 `plugins/self_learning.py`，YuKiKo 会自动检测并加载。

## 2. 首次配置

启动 YuKiKo 时，会自动进入配置向导：

```
┌─ SelfLearning 插件配置 ─┐
  启用自我学习功能? (Y/n): y

  沙盒模式:
    1. isolated - 完全隔离的沙盒环境（推荐）
    2. restricted - 受限环境，可访问部分系统资源
    3. full - 完全访问（危险，仅用于开发）
  选择 [1-3，默认 1]: 1

  自动测试:
    开启后 Agent 编写代码会自动在沙盒中测试
  启用自动测试? (Y/n): y

  DEVLOG 广播:
    开启后 Agent 会在群里发送学习日志（白话文）
  启用 DEVLOG 广播? (Y/n): y

  学习源:
    1. web - 从网络搜索学习（需要搜索工具）
    2. docs - 从文档学习
    3. both - 两者都用（默认）
  选择 [1-3，默认 3]: 3

  技能保存:
    开启后成功的技能会自动保存并可重用
  启用技能保存? (Y/n): y

  配置已保存到 plugins/config/self_learning.yml
└──────────────────────┘
```

## 3. 手动配置（可选）

编辑 `plugins/config/self_learning.yml`：

```yaml
enabled: true
sandbox_mode: isolated
auto_test: true
devlog_broadcast: true
learning_source: both
save_skills: true
max_learning_time_seconds: 300
max_code_lines: 500
test_timeout_seconds: 60
devlog_cooldown_seconds: 30
```

## 4. 验证安装

启动 YuKiKo 后，查看日志：

```
[INFO] self_learning setup | sandbox=isolated | auto_test=True | devlog=True
```

看到这条日志说明插件已成功加载。

## 5. 测试功能

在群里发送消息测试：

### 测试 1: 让 Agent 学习新技术

```
你: @YuKiKo 你会处理 Excel 文件吗？

YuKiKo: 让我学习一下...
        📚 DEVLOG | 我正在学习 Excel 处理，用 openpyxl 库看起来不错！
        💻 DEVLOG | 代码写好了，正在测试...
        ✅ DEVLOG | 学会了！现在我可以读写 Excel 文件了~
        是的，我现在会处理 Excel 了！需要我帮你做什么？
```

### 测试 2: 查看已学技能

```
你: @YuKiKo 你都学会了什么？

YuKiKo: 我现在会这些技能：
        1. json_parser - JSON 数据解析和格式化工具
        2. csv_handler - CSV 文件读写工具
        3. excel_processor - Excel 文件处理工具
        还在不断学习中！
```

### 测试 3: 让 Agent 创建自定义工具

```
你: @YuKiKo 帮我写个工具，能把文本转成语音

YuKiKo: 好的，让我学习一下文本转语音技术...
        📚 DEVLOG | 我在研究 TTS（文本转语音），pyttsx3 库看起来很好用！
        💻 DEVLOG | 正在编写代码...
        🧪 DEVLOG | 测试中...
        ✅ DEVLOG | 搞定！我写了个文本转语音工具，可以把文字变成声音~
        工具已经创建好了！要试试吗？
```

## 6. 查看创建的技能

技能保存在 `storage/self_created_skills/` 目录：

```
storage/self_created_skills/
├── json_parser.py          # 代码实现
├── json_parser.yml         # 元数据
├── csv_handler.py
├── csv_handler.yml
├── excel_processor.py
├── excel_processor.yml
└── ...
```

你可以查看和编辑这些文件。

## 7. 运行测试

验证插件功能：

```bash
cd D:\Project\YuKiKo\yukiko-bot
python tests/test_self_learning_plugin.py
```

预期输出：

```
test_plugin_initialization ... ok
test_needs_setup ... ok
test_learn_from_web_validation ... ok
test_create_skill_validation ... ok
test_sandbox_code_execution ... ok
test_devlog_cooldown ... ok
test_list_skills ... ok
test_code_line_limit ... ok
test_file_access_restriction ... ok
test_timeout_protection ... ok

----------------------------------------------------------------------
Ran 10 tests in 5.234s

OK
```

## 8. 常见问题

### Q: DEVLOG 没有发送到群里？

**A:** 检查配置：
1. `devlog_broadcast` 是否为 `true`
2. Agent 是否有群消息发送权限
3. 是否在冷却时间内（默认 30 秒）

### Q: 代码测试总是失败？

**A:** 可能原因：
1. 沙盒模式太严格 → 改为 `restricted`
2. 代码需要外部依赖 → 在沙盒中安装依赖
3. 超时时间太短 → 增加 `test_timeout_seconds`

### Q: 技能没有保存？

**A:** 检查：
1. `save_skills` 是否为 `true`
2. `storage/self_created_skills/` 目录权限
3. 磁盘空间是否充足

### Q: Agent 学习太慢？

**A:** 优化方法：
1. 减少 `max_learning_time_seconds`
2. 使用更快的搜索工具
3. 提供更具体的学习目标

## 9. 高级配置

### 自定义沙盒环境

编辑 `plugins/self_learning.py`，修改 `_test_code_in_sandbox` 方法：

```python
# 添加自定义环境变量
env = {
    "PATH": env.get("PATH", ""),
    "CUSTOM_VAR": "value",
}

# 添加自定义工作目录
cwd = "/custom/sandbox/path"
```

### 集成外部学习资源

在 `_handle_learn_from_web` 中添加：

```python
# 调用外部 API
if self._learning_source in ("web", "both"):
    search_results = await self._search_web(topic)

# 读取本地文档
if self._learning_source in ("docs", "both"):
    doc_content = await self._read_docs(topic)
```

### 技能版本控制

为技能添加版本管理：

```python
# 在元数据中添加版本号
meta = {
    "name": skill_name,
    "version": "1.0.0",
    "changelog": ["初始版本"],
}
```

## 10. 监控和日志

查看插件日志：

```bash
tail -f storage/logs/yukiko.log | grep self_learning
```

关键日志：

```
[INFO] self_learning setup | sandbox=isolated | auto_test=True
[INFO] skill_created | name=json_parser | lines=45
[INFO] devlog_sent | type=success | group=901738883
[WARNING] sandbox_test_failed | error=timeout
```

## 11. 性能优化

### 减少测试时间

```yaml
test_timeout_seconds: 30  # 从 60 降到 30
auto_test: false          # 禁用自动测试（不推荐）
```

### 限制资源使用

```yaml
max_code_lines: 200       # 限制代码行数
max_learning_time_seconds: 120  # 限制学习时间
```

### 批量学习

让 Agent 一次学习多个相关技术：

```python
topics = ["JSON 处理", "CSV 处理", "Excel 处理"]
for topic in topics:
    await learn_from_web(topic=topic, goal="数据处理")
```

## 12. 安全建议

1. **生产环境使用 isolated 模式**
   ```yaml
   sandbox_mode: isolated
   ```

2. **定期审查创建的技能**
   ```bash
   ls -la storage/self_created_skills/
   ```

3. **限制代码复杂度**
   ```yaml
   max_code_lines: 200
   ```

4. **监控异常行为**
   ```bash
   grep "ERROR\|WARNING" storage/logs/yukiko.log | grep self_learning
   ```

## 13. 下一步

- 阅读 [完整文档](SELF_LEARNING_README.md)
- 查看 [使用示例](SELF_LEARNING_EXAMPLES.py)
- 加入社区讨论改进建议

## 14. 获取帮助

- GitHub Issues: https://github.com/your-repo/issues
- 文档: [SELF_LEARNING_README.md](SELF_LEARNING_README.md)
- 示例: [SELF_LEARNING_EXAMPLES.py](SELF_LEARNING_EXAMPLES.py)

---

**祝你使用愉快！让 Agent 自己学习和成长吧~ 🚀**
