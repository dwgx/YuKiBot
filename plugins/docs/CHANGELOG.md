# 更新日志

本文档记录 SelfLearning 插件的所有重要变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 计划中
- 技能版本控制系统
- 技能市场功能
- 协作学习支持
- 知识图谱集成

## [1.0.0] - 2026-03-12

### 新增
- 🎉 初始版本发布
- ✨ 5 个核心 Agent 工具:
  - `learn_from_web`: 从网上学习新知识
  - `create_skill`: 创建新技能
  - `test_in_sandbox`: 沙盒测试代码
  - `send_devlog`: 发送开发日志
  - `list_my_skills`: 列出已创建技能
- 🔒 三级沙盒隔离模式 (isolated/restricted/full)
- 🧪 自动代码测试功能
- 📝 DEVLOG 白话文日志系统
- 💾 技能持久化存储
- 📊 性能统计和监控
- 🔍 重复技能检测
- ⚡ 技能缓存机制
- 📚 完整的文档系统:
  - README.md - 用户指南
  - API.md - API 文档
  - ARCHITECTURE.md - 架构文档
  - CONTRIBUTING.md - 贡献指南
  - EXAMPLES.py - 使用示例
  - QUICKSTART.md - 快速开始
- 🧪 完整的测试套件
- 🎨 交互式配置向导

### 安全
- 危险代码模式检测
- 进程隔离和超时保护
- 环境变量限制
- 文件系统访问控制
- 代码哈希验证

### 性能
- 异步代码执行
- 技能元数据缓存
- 优化的文件 I/O
- 会话管理优化

---

## 版本说明

### [1.0.0] - 初始发布

这是 SelfLearning 插件的第一个正式版本。经过充分的测试和文档编写，现在可以在生产环境中使用。

**核心功能:**

1. **自主学习**: Agent 可以从网上搜索和学习新知识
2. **技能创建**: 将学到的知识转化为可重用的代码技能
3. **安全测试**: 在隔离的沙盒环境中测试代码
4. **用户交流**: 用白话文向用户报告学习进展
5. **技能管理**: 保存、查看和重用已创建的技能

**安全特性:**

- 完全隔离的沙盒执行环境
- 危险操作自动检测
- 超时和资源限制保护
- 代码审查和验证

**性能优化:**

- 异步执行避免阻塞
- 智能缓存减少 I/O
- 并发会话支持
- 资源使用监控

**文档完善:**

- 详细的 API 文档
- 架构设计文档
- 贡献指南
- 丰富的使用示例
- 快速开始指南

**测试覆盖:**

- 单元测试
- 集成测试
- 安全测试
- 性能测试

---

## 升级指南

### 从开发版升级到 1.0.0

如果你之前使用过开发版本，请按以下步骤升级：

1. **备份数据**
   ```bash
   cp -r storage/self_created_skills storage/self_created_skills.backup
   ```

2. **更新代码**
   ```bash
   git pull origin main
   ```

3. **更新配置**
   ```bash
   # 检查新的配置选项
   diff plugins/config/self_learning.yml plugins/config/self_learning.template.yml

   # 根据需要更新配置
   vim plugins/config/self_learning.yml
   ```

4. **运行测试**
   ```bash
   python tests/test_self_learning_plugin.py
   ```

5. **重启 YuKiKo**
   ```bash
   python main.py
   ```

---

## 已知问题

### v1.0.0

**沙盒限制:**
- Windows 上的沙盒隔离不如 Linux 完善
- 某些系统调用可能无法完全隔离

**性能:**
- 大型代码文件的测试可能较慢
- 并发会话数量受系统资源限制

**功能限制:**
- 暂不支持技能版本控制
- 暂不支持技能依赖管理
- 暂不支持跨 Agent 技能共享

---

## 路线图

### v1.1.0 (计划中)

**新功能:**
- [ ] 技能版本控制
- [ ] 技能依赖管理
- [ ] 改进的学习算法
- [ ] 更多学习源支持

**改进:**
- [ ] 优化沙盒性能
- [ ] 增强安全检测
- [ ] 改进错误提示
- [ ] 更好的日志系统

**文档:**
- [ ] 视频教程
- [ ] 更多示例
- [ ] 最佳实践指南

### v1.2.0 (计划中)

**新功能:**
- [ ] 技能市场
- [ ] 协作学习
- [ ] 知识图谱
- [ ] 自动优化

**改进:**
- [ ] 分布式执行
- [ ] 更好的缓存策略
- [ ] 性能监控面板

### v2.0.0 (远期)

**重大变更:**
- [ ] 全新的学习引擎
- [ ] 联邦学习支持
- [ ] 元学习能力
- [ ] 自我进化系统

---

## 贡献者

感谢所有为这个项目做出贡献的人！

- [@YuKiKo-Team](https://github.com/yukiko-team) - 核心开发
- 以及所有提交 Issue 和 PR 的贡献者

---

## 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](../../LICENSE) 文件

---

## 联系方式

- **Issues**: [GitHub Issues](https://github.com/your-repo/issues)
- **讨论**: [GitHub Discussions](https://github.com/your-repo/discussions)
- **邮件**: yukiko@example.com

---

**注意**: 本更新日志遵循 [Keep a Changelog](https://keepachangelog.com/) 规范。

[未发布]: https://github.com/your-repo/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/your-repo/releases/tag/v1.0.0
