# SelfLearning 插件 - 文件清单

本文档列出了 SelfLearning 插件的所有文件及其说明。

## 📁 文件结构

```
yukiko-bot/
├── plugins/
│   ├── self_learning.py                    # 主插件代码
│   ├── SELF_LEARNING_README.md             # 用户指南
│   ├── SELF_LEARNING_EXAMPLES.py           # 使用示例
│   ├── QUICKSTART.md                       # 快速开始
│   ├── PROJECT_SUMMARY.md                  # 项目总结
│   ├── config/
│   │   └── self_learning.template.yml      # 配置模板
│   └── docs/
│       ├── README.md                       # 文档中心
│       ├── API.md                          # API 文档
│       ├── ARCHITECTURE.md                 # 架构文档
│       ├── CONTRIBUTING.md                 # 贡献指南
│       ├── MAINTENANCE.md                  # 维护指南
│       └── CHANGELOG.md                    # 更新日志
├── storage/
│   └── self_created_skills/
│       ├── example_json_parser.py          # 示例技能代码
│       └── example_json_parser.yml         # 示例技能元数据
└── tests/
    └── test_self_learning_plugin.py        # 测试文件
```

## 📄 文件详情

### 核心代码

#### `plugins/self_learning.py`

**类型**: Python 代码
**大小**: ~30 KB
**行数**: ~900 行

**内容**:
- Plugin 类实现
- LearningSession 数据类
- 5 个 Agent 工具处理器
- 沙盒执行系统
- 技能管理系统
- 配置向导

**关键类和函数**:
- `Plugin` - 主插件类
- `LearningSession` - 学习会话
- `_handle_learn_from_web()` - 学习工具
- `_handle_create_skill()` - 创建技能
- `_handle_test_in_sandbox()` - 沙盒测试
- `_handle_send_devlog()` - 发送日志
- `_handle_list_skills()` - 列出技能
- `_test_code_in_sandbox()` - 沙盒执行
- `_load_skill_cache()` - 加载缓存
- `_sanitize_code()` - 代码清理

---

### 配置文件

#### `plugins/config/self_learning.template.yml`

**类型**: YAML 配置
**大小**: ~1 KB

**内容**:
- 基础配置选项
- 沙盒模式设置
- 学习源配置
- 性能参数
- DEVLOG 设置

**配置项**:
```yaml
enabled: boolean
sandbox_mode: string
auto_test: boolean
devlog_broadcast: boolean
learning_source: string
save_skills: boolean
max_learning_time_seconds: number
max_code_lines: number
test_timeout_seconds: number
devlog_cooldown_seconds: number
```

---

### 测试文件

#### `tests/test_self_learning_plugin.py`

**类型**: Python 测试
**大小**: ~8 KB
**行数**: ~300 行

**内容**:
- 单元测试
- 集成测试
- 安全测试
- 性能测试

**测试类**:
- `TestSelfLearningPlugin` - 功能测试
- `TestSandboxSecurity` - 安全测试

**测试用例**:
- `test_plugin_initialization()` - 初始化测试
- `test_needs_setup()` - 配置检查测试
- `test_learn_from_web_validation()` - 学习工具测试
- `test_create_skill_validation()` - 技能创建测试
- `test_sandbox_code_execution()` - 沙盒执行测试
- `test_devlog_cooldown()` - DEVLOG 冷却测试
- `test_list_skills()` - 技能列表测试
- `test_code_line_limit()` - 行数限制测试
- `test_file_access_restriction()` - 文件访问测试
- `test_timeout_protection()` - 超时保护测试

---

### 示例文件

#### `storage/self_created_skills/example_json_parser.py`

**类型**: Python 代码
**大小**: ~2 KB
**行数**: ~80 行

**内容**:
- JSON 解析函数
- JSON 格式化函数
- 字段提取函数
- 测试代码

**函数**:
- `parse_json()` - 解析 JSON
- `format_json()` - 格式化 JSON
- `extract_field()` - 提取字段

#### `storage/self_created_skills/example_json_parser.yml`

**类型**: YAML 元数据
**大小**: ~800 B

**内容**:
- 技能基本信息
- 标签和依赖
- 使用示例
- 性能指标

---

### 用户文档

#### `plugins/SELF_LEARNING_README.md`

**类型**: Markdown 文档
**大小**: ~7 KB
**字数**: ~5,000 字

**章节**:
1. 功能概述
2. 工作流程
3. Agent 工具
4. 使用场景
5. 配置说明
6. 安全考虑
7. 监控和日志
8. 故障排除

#### `plugins/QUICKSTART.md`

**类型**: Markdown 文档
**大小**: ~7 KB
**字数**: ~3,000 字

**章节**:
1. 安装插件
2. 首次配置
3. 手动配置
4. 验证安装
5. 测试功能
6. 查看技能
7. 运行测试
8. 常见问题
9. 高级配置
10. 监控和日志
11. 性能优化
12. 安全建议
13. 下一步
14. 获取帮助

#### `plugins/SELF_LEARNING_EXAMPLES.py`

**类型**: Python 示例
**大小**: ~9 KB
**字数**: ~2,500 字

**示例**:
1. Agent 学习新技术
2. Agent 自我改进
3. Agent 学习用户需求
4. Agent 查看技能
5. Agent 主动学习
6. 完整对话示例

---

### 开发者文档

#### `plugins/docs/API.md`

**类型**: Markdown 文档
**大小**: ~15 KB
**字数**: ~8,000 字

**章节**:
1. Agent 工具 API
   - learn_from_web
   - create_skill
   - test_in_sandbox
   - send_devlog
   - list_my_skills
2. 插件类 API
3. 数据模型
4. 配置 API
5. 错误处理
6. 使用示例

#### `plugins/docs/ARCHITECTURE.md`

**类型**: Markdown 文档
**大小**: ~17 KB
**字数**: ~10,000 字

**章节**:
1. 概述
2. 系统架构
3. 核心组件
4. 数据流
5. 安全模型
6. 性能优化
7. 扩展点
8. 配置管理
9. 监控和日志
10. 测试策略
11. 部署建议
12. 未来规划

#### `plugins/docs/CONTRIBUTING.md`

**类型**: Markdown 文档
**大小**: ~13 KB
**字数**: ~6,000 字

**章节**:
1. 行为准则
2. 如何贡献
3. 开发环境设置
4. 代码规范
5. 提交规范
6. 测试要求
7. 文档要求
8. Pull Request 流程
9. 开发技巧
10. 发布流程
11. 获取帮助
12. 致谢

---

### 维护文档

#### `plugins/docs/MAINTENANCE.md`

**类型**: Markdown 文档
**大小**: ~8 KB
**字数**: ~4,000 字

**章节**:
1. 日常维护
2. 版本发布
3. 问题处理
4. 性能监控
5. 安全审计
6. 数据库维护
7. 监控和告警
8. 文档维护
9. 社区管理
10. 备份和恢复
11. 故障排查

#### `plugins/docs/CHANGELOG.md`

**类型**: Markdown 文档
**大小**: ~5 KB
**字数**: ~2,000 字

**章节**:
1. 版本历史
2. 版本说明
3. 升级指南
4. 已知问题
5. 路线图
6. 贡献者
7. 许可证
8. 联系方式

#### `plugins/docs/README.md`

**类型**: Markdown 文档
**大小**: ~7 KB
**字数**: ~2,000 字

**章节**:
1. 文档导航
2. 快速链接
3. 文档概览
4. 学习路径
5. 文档更新
6. 搜索文档
7. 常见问题
8. 文档统计
9. 其他资源
10. 反馈
11. 许可证
12. 致谢

---

### 项目文档

#### `plugins/PROJECT_SUMMARY.md`

**类型**: Markdown 文档
**大小**: ~8 KB
**字数**: ~3,000 字

**章节**:
1. 已完成的内容
2. 项目统计
3. 核心特性
4. 安全特性
5. 性能优化
6. 文档完善度
7. 测试覆盖
8. 代码质量
9. 部署就绪
10. 项目亮点
11. 技术栈
12. 使用场景
13. 未来展望
14. 项目成就

---

## 📊 统计信息

### 代码文件

| 文件 | 类型 | 大小 | 行数 | 说明 |
|------|------|------|------|------|
| self_learning.py | Python | ~30 KB | ~900 | 主插件代码 |
| test_self_learning_plugin.py | Python | ~8 KB | ~300 | 测试代码 |
| example_json_parser.py | Python | ~2 KB | ~80 | 示例技能 |
| SELF_LEARNING_EXAMPLES.py | Python | ~9 KB | ~250 | 使用示例 |
| **总计** | - | **~49 KB** | **~1,530** | - |

### 配置文件

| 文件 | 类型 | 大小 | 行数 | 说明 |
|------|------|------|------|------|
| self_learning.template.yml | YAML | ~1 KB | ~30 | 配置模板 |
| example_json_parser.yml | YAML | ~800 B | ~25 | 技能元数据 |
| **总计** | - | **~2 KB** | **~55** | - |

### 文档文件

| 文件 | 类型 | 大小 | 字数 | 说明 |
|------|------|------|------|------|
| SELF_LEARNING_README.md | Markdown | ~7 KB | ~5,000 | 用户指南 |
| QUICKSTART.md | Markdown | ~7 KB | ~3,000 | 快速开始 |
| API.md | Markdown | ~15 KB | ~8,000 | API 文档 |
| ARCHITECTURE.md | Markdown | ~17 KB | ~10,000 | 架构文档 |
| CONTRIBUTING.md | Markdown | ~13 KB | ~6,000 | 贡献指南 |
| MAINTENANCE.md | Markdown | ~8 KB | ~4,000 | 维护指南 |
| CHANGELOG.md | Markdown | ~5 KB | ~2,000 | 更新日志 |
| docs/README.md | Markdown | ~7 KB | ~2,000 | 文档中心 |
| PROJECT_SUMMARY.md | Markdown | ~8 KB | ~3,000 | 项目总结 |
| **总计** | - | **~87 KB** | **~43,000** | - |

### 总计

| 类型 | 文件数 | 大小 | 行数/字数 |
|------|--------|------|----------|
| Python 代码 | 4 | ~49 KB | ~1,530 行 |
| YAML 配置 | 2 | ~2 KB | ~55 行 |
| Markdown 文档 | 9 | ~87 KB | ~43,000 字 |
| **总计** | **15** | **~138 KB** | **~1,585 行 + 43,000 字** |

---

## 🎯 文件用途

### 给用户

- `SELF_LEARNING_README.md` - 了解功能
- `QUICKSTART.md` - 快速上手
- `SELF_LEARNING_EXAMPLES.py` - 学习使用
- `CHANGELOG.md` - 查看更新

### 给开发者

- `self_learning.py` - 阅读代码
- `API.md` - 查看 API
- `ARCHITECTURE.md` - 理解架构
- `CONTRIBUTING.md` - 参与贡献

### 给维护者

- `MAINTENANCE.md` - 日常维护
- `test_self_learning_plugin.py` - 运行测试
- `PROJECT_SUMMARY.md` - 项目概览
- `docs/README.md` - 文档导航

---

## 📝 文件关系

```
用户入口
    ↓
QUICKSTART.md → SELF_LEARNING_README.md → SELF_LEARNING_EXAMPLES.py
    ↓                    ↓                           ↓
开发者入口              API.md                   实际使用
    ↓                    ↓                           ↓
CONTRIBUTING.md → ARCHITECTURE.md → self_learning.py
    ↓                    ↓                           ↓
维护者入口          系统设计                    代码实现
    ↓                    ↓                           ↓
MAINTENANCE.md → test_self_learning_plugin.py → 测试验证
```

---

## 🔍 快速查找

### 我想...

- **安装插件** → `QUICKSTART.md` 第 1 节
- **配置插件** → `QUICKSTART.md` 第 2 节
- **了解功能** → `SELF_LEARNING_README.md`
- **查看示例** → `SELF_LEARNING_EXAMPLES.py`
- **调用 API** → `API.md`
- **理解架构** → `ARCHITECTURE.md`
- **参与开发** → `CONTRIBUTING.md`
- **维护系统** → `MAINTENANCE.md`
- **查看更新** → `CHANGELOG.md`
- **阅读代码** → `self_learning.py`
- **运行测试** → `test_self_learning_plugin.py`

---

## 📦 打包清单

发布时需要包含的文件：

### 必需文件

- ✅ `self_learning.py`
- ✅ `self_learning.template.yml`
- ✅ `SELF_LEARNING_README.md`
- ✅ `QUICKSTART.md`

### 推荐文件

- ✅ `SELF_LEARNING_EXAMPLES.py`
- ✅ `docs/API.md`
- ✅ `docs/ARCHITECTURE.md`
- ✅ `docs/CONTRIBUTING.md`
- ✅ `docs/CHANGELOG.md`

### 可选文件

- ✅ `docs/MAINTENANCE.md`
- ✅ `docs/README.md`
- ✅ `PROJECT_SUMMARY.md`
- ✅ `test_self_learning_plugin.py`
- ✅ `example_json_parser.py`
- ✅ `example_json_parser.yml`

---

## 🔄 文件更新

### 需要同步更新的文件

当修改功能时，需要更新：

1. **代码变更**:
   - `self_learning.py`
   - `test_self_learning_plugin.py`
   - `API.md`

2. **配置变更**:
   - `self_learning.template.yml`
   - `QUICKSTART.md`
   - `SELF_LEARNING_README.md`

3. **新增功能**:
   - `self_learning.py`
   - `API.md`
   - `SELF_LEARNING_README.md`
   - `SELF_LEARNING_EXAMPLES.py`
   - `CHANGELOG.md`

4. **架构变更**:
   - `ARCHITECTURE.md`
   - `API.md`
   - `CONTRIBUTING.md`

---

**最后更新**: 2026-03-12
**文件版本**: v1.0.0
