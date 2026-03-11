"""YuKiKo 插件开发模板 & 完整指南。

本文件既是可运行的示例插件，也是插件开发文档。
插件完全独立于 YuKiKo 主项目 —— 你不需要修改任何核心代码。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
插件系统概览
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

放置位置:  plugins/<your_plugin>.py
配置文件:  plugins/config/<your_plugin>.yml  (可选，优先于 config.yml)
发现规则:  自动扫描 plugins/*.py，跳过 _开头 和 __init__.py

生命周期:
1. 发现 → import 模块，找到 Plugin 类
2. 首次配置 → needs_setup() → interactive_setup()  (可选)
3. 实例化 → Plugin()
4. 初始化 → setup(config, context)  (可选，异步)
5. 运行中 → handle(message, context)  被 Router/Agent 调用
6. 关闭 → teardown()  (可选，异步)

插件能力:
- handle()        : 处理用户消息，返回文本回复
- Agent 工具注册  : 通过 context.agent_tool_registry 注册工具让 Agent 自主调用
- Prompt 注入     : 注册 PromptHint 影响 Agent 的系统提示词
- 动态上下文      : 注册 ContextProvider 在每次对话时注入实时信息
- 独立配置        : plugins/config/<name>.yml 独立管理，热重载友好
- 交互式向导      : needs_setup() + interactive_setup() 首次运行引导

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("yukiko.plugin.example")

# ──────────────────────────────────────────────────────────────────────────────
# 插件类定义
# ──────────────────────────────────────────────────────────────────────────────


class Plugin:
    """示例插件 —— 展示所有可用的插件接口。

    必须属性:
        name            : str          插件唯一标识 (与文件名对应)
        description     : str          功能描述 (Router 用来判断是否调用)
        intent_examples : list[str]    触发意图示例 (帮助 Router 识别)
        rules           : list[str]    行为约束 (注入到 Agent/Router 提示词)
        args_schema     : dict         参数说明 (告诉 Agent 如何传参)

    可选属性:
        agent_tool      : bool = False   True 时注册为 Agent 内部工具，不走 Router
        internal_only   : bool = False   True 时对 Router schema 隐藏
    """

    # ── 必须属性 ──
    name = "example"
    description = "示例插件，演示 /ping、/echo、/time 命令。"

    intent_examples = [
        "调用 example 插件",
        "帮我 ping 一下",
        "回显这段话",
        "现在几点了",
    ]

    rules = [
        "仅处理轻量文本请求，不执行系统命令。",
        "不写本地文件，不读取隐私信息。",
        "优先简短回复，避免刷屏。",
    ]

    args_schema = {
        "message": "string，用户的原始消息文本",
    }

    # ── 可选属性 ──
    # agent_tool = True       # 取消注释 → 变成 Agent 内部工具
    # internal_only = True    # 取消注释 → Router 不可见

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # [可选] 首次配置向导
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 如果你的插件需要 API key、外部服务地址等配置，可以实现这两个静态方法。
    # 引擎启动时会检查 needs_setup()，为 True 则调用 interactive_setup()。
    # 配置结果保存到 plugins/config/<name>.yml，下次启动不再触发。

    # @staticmethod
    # def needs_setup() -> bool:
    #     """配置文件不存在时返回 True。"""
    #     from pathlib import Path
    #     return not (Path(__file__).parent / "config" / "example.yml").exists()
    #
    # @staticmethod
    # def interactive_setup() -> dict[str, Any]:
    #     """交互式向导，返回配置 dict (会被自动保存)。"""
    #     print("┌─ Example 插件配置 ─┐")
    #     api_key = input("  API Key (留空跳过): ").strip()
    #     cfg = {"enabled": True, "api_key": api_key}
    #     # 保存到 plugins/config/example.yml
    #     from pathlib import Path
    #     import yaml
    #     config_dir = Path(__file__).parent / "config"
    #     config_dir.mkdir(exist_ok=True)
    #     with open(config_dir / "example.yml", "w", encoding="utf-8") as f:
    #         yaml.dump(cfg, f, allow_unicode=True)
    #     print("└──────────────────┘")
    #     return cfg

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # [可选] setup() — 引擎初始化时调用
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # config: 来自 plugins/config/example.yml 或 config.yml → plugins.example
    # context: PluginSetupContext，包含:
    #   - context.model_client          : LLM 客户端 (可调用模型)
    #   - context.config                : 全局配置 dict
    #   - context.logger                : 日志实例
    #   - context.storage_dir           : 持久化存储目录 (Path)
    #   - context.agent_tool_registry   : Agent 工具注册中心 (核心!)

    async def setup(self, config: dict[str, Any], context: Any) -> None:
        """初始化插件，注册 Agent 工具和提示词。"""
        self._config = config
        _log.info("example plugin setup | config_keys=%s", list(config.keys()))

        # 获取 Agent 工具注册中心
        registry = getattr(context, "agent_tool_registry", None)
        if registry is not None:
            self._register_agent_tools(registry)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # [核心] handle() — 处理用户消息 (必须实现)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # message: 用户消息文本
    # context: dict，包含:
    #   - user_id       : str   用户 QQ 号
    #   - user_name     : str   用户昵称
    #   - group_id      : str   群号 (私聊为空)
    #   - is_private    : bool  是否私聊
    #   - message_text  : str   原始消息文本

    async def handle(self, message: str, context: dict) -> str:
        """处理用户消息，返回文本回复。"""
        text = (message or "").strip()
        user_name = str(context.get("user_name", "用户")).strip() or "用户"

        if text.lower().startswith("/ping"):
            return "在线。"

        if text.lower().startswith("/echo "):
            content = text[6:].strip()
            return content or "echo 为空。"

        if text.lower().startswith("/time"):
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return f"当前时间: {now}"

        return f"示例插件已触发。你好，{user_name}。可用命令: /ping, /echo <文本>, /time"

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # [可选] teardown() — 引擎关闭时调用
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def teardown(self) -> None:
        """清理资源。关闭连接、取消定时器等。"""
        _log.info("example plugin teardown")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # [高级] 注册 Agent 工具
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Agent 工具让 AI 自主决定何时调用你的插件功能。
    # 与 handle() 不同，Agent 工具由 AI 根据用户意图自动触发。
    #
    # 需要导入:
    #   from core.agent_tools import ToolSchema, ToolCallResult, PromptHint

    def _register_agent_tools(self, registry: Any) -> None:
        """注册 Agent 可调用的工具、提示词和上下文。"""
        from core.agent_tools import ToolSchema, ToolCallResult, PromptHint

        # ── 1. 注册工具 ──
        # ToolSchema 定义工具的名称、描述、参数 (JSON Schema 格式)
        # handler 是 async 函数，签名: async def handler(args: dict, context: dict) -> ToolCallResult

        registry.register(
            ToolSchema(
                name="example_lookup",
                description="示例查询工具，根据关键词返回模拟数据。仅用于演示。",
                parameters={
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "查询关键词",
                        },
                    },
                    "required": ["keyword"],
                },
                category="general",  # 可选: general / napcat / search / media / admin / cli
            ),
            self._handle_example_lookup,
        )

        # ── 2. 注册 PromptHint (静态提示词注入) ──
        # section 可选值:
        #   "rules"           → 注入到 Agent 的 ## 规则 区域
        #   "tools_guidance"  → 注入到 ## 工具使用指南 区域
        #   "context"         → 注入到 ## 上下文 区域
        # priority: 数字越小越靠前 (默认 50)

        registry.register_prompt_hint(PromptHint(
            source="example",
            section="rules",
            content="example_lookup 仅用于演示，不要在正式对话中使用。",
            priority=90,  # 低优先级
        ))

        # ── 3. 注册动态上下文 (每次对话时实时生成) ──
        # provider 签名: Callable[[dict], str | Awaitable[str]]
        # 参数 info 包含: user_id, group_id, is_private, mentioned

        registry.register_context_provider(
            "example_status",
            lambda info: "示例插件状态: 正常运行中。",
            priority=90,
        )

        _log.info("example plugin: agent tools registered")

    @staticmethod
    async def _handle_example_lookup(
        args: dict[str, Any], context: dict[str, Any],
    ) -> Any:
        """Agent 工具 handler 示例。"""
        from core.agent_tools import ToolCallResult

        keyword = str(args.get("keyword", "")).strip()
        if not keyword:
            return ToolCallResult(ok=False, error="missing_keyword", display="请提供关键词")

        # 你的业务逻辑放这里 (API 调用、数据库查询等)
        result_text = f"查询 '{keyword}' 的模拟结果: 共找到 3 条记录。"

        return ToolCallResult(
            ok=True,
            data={"keyword": keyword, "count": 3},  # 结构化数据 (可选)
            display=result_text,  # 给 Agent 看的摘要文本
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 插件开发速查
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# 最小插件 (只需 handle):
#
#   class Plugin:
#       name = "my_plugin"
#       description = "做某件事"
#       intent_examples = ["触发示例"]
#       rules = ["行为约束"]
#       args_schema = {"message": "string"}
#
#       async def handle(self, message: str, context: dict) -> str:
#           return "回复内容"
#
# ─────────────────────────────────────────────────────────────────────────────
#
# Agent 工具插件 (AI 自主调用):
#
#   class Plugin:
#       name = "my_tool"
#       description = "..."
#       agent_tool = True          # 标记为 Agent 工具
#       internal_only = True       # Router 不可见
#       intent_examples = []
#       rules = []
#       args_schema = {}
#
#       async def setup(self, config, context):
#           reg = context.agent_tool_registry
#           reg.register(ToolSchema(...), self._handler)
#           reg.register_prompt_hint(PromptHint(...))
#
#       async def handle(self, message, context):
#           return "此插件仅供 Agent 内部使用。"
#
# ─────────────────────────────────────────────────────────────────────────────
#
# 配置文件 (plugins/config/my_plugin.yml):
#
#   enabled: true
#   api_key: "xxx"
#   custom_setting: 42
#
#   → setup(config, context) 的 config 参数就是这个 dict
#   → 优先级: plugins/config/xxx.yml > config.yml → plugins.xxx
#
# ─────────────────────────────────────────────────────────────────────────────
#
# 关键原则:
#   1. 插件完全独立 —— 不需要修改 YuKiKo 主项目任何代码
#   2. 一个 .py 文件 = 一个插件，放到 plugins/ 目录即可
#   3. 通过 agent_tool_registry 注册工具，Agent 会自动发现并使用
#   4. 通过 PromptHint 影响 Agent 行为，无需改 Agent 代码
#   5. 通过 ContextProvider 注入实时信息，Agent 每次对话都能看到
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
