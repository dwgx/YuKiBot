# 文档索引

## 目录约定

- `README.md`：只放安装、启动、最常见使用入口。
- `docs/architecture/`：放长期有效的架构说明与主链路设计。
- `docs/features/`：放单一功能域的行为说明、配置语义、回退策略。
- `docs/ops/`：放部署、值班、故障排查与运行手册。
- `docs/archive/`：放一次性修复记录、阶段报告、临时排查纪要。

## 当前核心文档

- `docs/architecture/trigger-routing.md`：说明当前触发/路由已移除本地关键词旁听路径。
- `docs/features/music.md`：说明音乐链路、`unblock_sources` 与 `alternative_sources` 的职责边界。

## 维护规则

- 根目录不要堆临时修复报告。
- 单次排查类文档优先放 `docs/archive/按日期分目录`。
- 配置语义变更时，先更新 `docs/features/`，再更新模板与测试。
