# 维护指南

本文档为 SelfLearning 插件的维护者提供指导。

## 目录

- [日常维护](#日常维护)
- [版本发布](#版本发布)
- [问题处理](#问题处理)
- [性能监控](#性能监控)
- [安全审计](#安全审计)
- [数据库维护](#数据库维护)

## 日常维护

### 检查日志

每天检查日志文件，查找异常：

```bash
# 查看最近的错误
tail -n 100 storage/logs/yukiko.log | grep "ERROR\|WARNING" | grep self_learning

# 查看今天的日志
grep "$(date +%Y-%m-%d)" storage/logs/yukiko.log | grep self_learning

# 统计错误类型
grep "ERROR" storage/logs/yukiko.log | grep self_learning | awk '{print $NF}' | sort | uniq -c
```

### 监控性能

定期检查性能指标：

```python
# 获取统计信息
stats = plugin.get_stats()
print(f"成功率: {stats['success_rate']:.2%}")
print(f"总测试: {stats['total_tests']}")
print(f"活跃会话: {stats['active_sessions']}")
```

### 清理过期数据

```bash
# 清理超过 30 天的沙盒临时文件
find storage/sandbox -type f -mtime +30 -delete

# 清理失败的技能（可选）
# 手动检查后删除
```

### 更新依赖

```bash
# 检查过时的依赖
pip list --outdated

# 更新依赖
pip install --upgrade package_name

# 运行测试确保兼容性
pytest
```

## 版本发布

### 发布检查清单

- [ ] 所有测试通过
- [ ] 文档已更新
- [ ] CHANGELOG 已更新
- [ ] 版本号已更新
- [ ] 没有已知的严重 bug

### 发布步骤

1. **更新版本号**

```python
# plugins/self_learning.py
__version__ = "1.1.0"
```

2. **更新 CHANGELOG**

```markdown
## [1.1.0] - 2026-04-15

### 新增
- 技能版本控制
- ...
```

3. **创建 Git Tag**

```bash
git tag -a v1.1.0 -m "Release version 1.1.0"
git push origin v1.1.0
```

4. **创建 GitHub Release**

- 访问 GitHub Releases 页面
- 点击 "Draft a new release"
- 选择 tag v1.1.0
- 填写发布说明
- 发布

5. **通知用户**

- 在社区发布公告
- 更新文档网站
- 发送邮件通知（如适用）

## 问题处理

### Issue 分类

使用标签分类 Issue：

- `bug`: Bug 报告
- `enhancement`: 功能请求
- `documentation`: 文档相关
- `question`: 问题咨询
- `security`: 安全问题
- `performance`: 性能问题

### 优先级

- `P0`: 严重 bug，立即修复
- `P1`: 重要 bug，本周修复
- `P2`: 一般 bug，本月修复
- `P3`: 小问题，有空修复

### 响应时间

- 安全问题: 24 小时内响应
- 严重 bug: 48 小时内响应
- 一般问题: 1 周内响应
- 功能请求: 2 周内响应

### Bug 修复流程

1. **确认 Bug**
   - 尝试复现
   - 收集更多信息
   - 确定影响范围

2. **修复**
   - 创建修复分支
   - 编写修复代码
   - 添加测试用例
   - 更新文档

3. **测试**
   - 运行所有测试
   - 手动测试
   - 请报告者验证

4. **发布**
   - 合并到主分支
   - 发布补丁版本
   - 更新 CHANGELOG

## 性能监控

### 关键指标

监控以下指标：

```python
# 成功率
success_rate = passed_tests / total_tests

# 平均执行时间
avg_execution_time = sum(execution_times) / len(execution_times)

# 活跃会话数
active_sessions = len(plugin._sessions)

# 技能数量
total_skills = len(plugin._skill_cache)
```

### 性能基准

建立性能基准：

```python
# tests/benchmark.py
import time

def benchmark_create_skill():
    start = time.time()
    # 创建技能
    elapsed = time.time() - start
    assert elapsed < 5.0  # 应该在 5 秒内完成

def benchmark_sandbox_test():
    start = time.time()
    # 测试代码
    elapsed = time.time() - start
    assert elapsed < 2.0  # 应该在 2 秒内完成
```

### 性能优化

如果性能下降：

1. **分析瓶颈**
   ```bash
   python -m cProfile -o profile.stats plugins/self_learning.py
   python -m pstats profile.stats
   ```

2. **优化代码**
   - 减少不必要的 I/O
   - 使用缓存
   - 并发执行
   - 优化算法

3. **验证改进**
   - 运行基准测试
   - 比较前后性能
   - 确保功能正常

## 安全审计

### 定期审计

每月进行安全审计：

1. **代码审查**
   - 检查新增代码
   - 查找安全漏洞
   - 更新危险模式列表

2. **依赖检查**
   ```bash
   # 检查已知漏洞
   pip-audit

   # 或使用 safety
   safety check
   ```

3. **沙盒测试**
   - 尝试各种逃逸方法
   - 测试资源限制
   - 验证隔离效果

### 安全事件响应

如果发现安全问题：

1. **评估严重性**
   - 低: 理论风险，难以利用
   - 中: 需要特定条件才能利用
   - 高: 容易利用，影响大
   - 严重: 正在被利用

2. **立即行动**
   - 严重问题: 立即发布补丁
   - 高危问题: 24 小时内修复
   - 中危问题: 1 周内修复
   - 低危问题: 下个版本修复

3. **通知用户**
   - 发布安全公告
   - 说明影响范围
   - 提供缓解措施
   - 发布修复版本

### 安全最佳实践

- 最小权限原则
- 输入验证
- 输出编码
- 安全的默认配置
- 定期更新依赖

## 数据库维护

### 技能数据库

```bash
# 备份技能
tar -czf skills_backup_$(date +%Y%m%d).tar.gz storage/self_created_skills/

# 验证技能完整性
python scripts/verify_skills.py

# 清理损坏的技能
python scripts/cleanup_skills.py
```

### 会话数据

```python
# 清理过期会话
def cleanup_expired_sessions():
    now = datetime.now()
    expired = []
    for session_id, session in plugin._sessions.items():
        if (now - session.start_time).days > 7:
            expired.append(session_id)
    for session_id in expired:
        del plugin._sessions[session_id]
```

### 缓存管理

```python
# 重建缓存
plugin._skill_cache.clear()
plugin._cache_loaded = False
plugin._load_skill_cache()

# 验证缓存一致性
def verify_cache():
    disk_skills = set(f.stem for f in _SKILLS_DIR.glob("*.yml"))
    cache_skills = set(plugin._skill_cache.keys())
    assert disk_skills == cache_skills
```

## 监控和告警

### 设置告警

```python
# 监控脚本
def check_health():
    stats = plugin.get_stats()

    # 成功率过低
    if stats['success_rate'] < 0.8:
        send_alert("成功率低于 80%")

    # 活跃会话过多
    if stats['active_sessions'] > 10:
        send_alert("活跃会话超过 10 个")

    # 磁盘空间不足
    disk_usage = shutil.disk_usage(_SKILLS_DIR)
    if disk_usage.free < 1024 * 1024 * 1024:  # 1GB
        send_alert("磁盘空间不足 1GB")
```

### 日志分析

```bash
# 每日日志报告
cat storage/logs/yukiko.log | \
  grep "$(date +%Y-%m-%d)" | \
  grep self_learning | \
  awk '{print $4}' | \
  sort | uniq -c | \
  sort -rn > daily_report.txt
```

## 文档维护

### 定期更新

- 每个版本更新 CHANGELOG
- 新功能添加到 README
- API 变更更新 API 文档
- 添加新的使用示例

### 文档审查

每季度审查文档：

- 检查过时信息
- 更新截图
- 修正错误
- 改进表达

## 社区管理

### 响应社区

- 及时回复 Issue
- 审查 Pull Request
- 参与讨论
- 感谢贡献者

### 社区活动

- 定期发布进展
- 举办线上活动
- 收集用户反馈
- 改进用户体验

## 备份和恢复

### 备份策略

```bash
# 每日备份
0 2 * * * /path/to/backup.sh

# backup.sh
#!/bin/bash
DATE=$(date +%Y%m%d)
tar -czf backup_$DATE.tar.gz \
  storage/self_created_skills/ \
  plugins/config/self_learning.yml
```

### 恢复流程

```bash
# 恢复技能
tar -xzf backup_20260312.tar.gz -C /

# 验证
python scripts/verify_skills.py

# 重启服务
systemctl restart yukiko
```

## 故障排查

### 常见问题

1. **沙盒测试失败**
   - 检查 Python 环境
   - 验证权限设置
   - 查看详细日志

2. **技能保存失败**
   - 检查磁盘空间
   - 验证目录权限
   - 检查文件系统

3. **DEVLOG 发送失败**
   - 检查网络连接
   - 验证 API 权限
   - 查看冷却时间

### 调试技巧

```python
# 启用详细日志
logging.getLogger("yukiko.plugin.self_learning").setLevel(logging.DEBUG)

# 使用调试器
import pdb; pdb.set_trace()

# 性能分析
import cProfile
cProfile.run('plugin.create_skill(...)')
```

## 联系方式

维护者联系方式：

- **主要维护者**: @maintainer
- **邮件**: maintainer@example.com
- **紧急联系**: +1234567890

---

**最后更新**: 2026-03-12
