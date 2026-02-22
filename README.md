# YukikoBot（雪）

## 项目简介

`YukikoBot` 是基于 `NapCat (OneBot11) + NoneBot2 + Python` 的 QQ 机器人框架。  
项目重点是模块化：触发判断、思考决策、联网搜索、长期记忆、图片生成、插件扩展彼此解耦，便于后续升级。

## 目录说明

- `main.py`：程序启动入口
- `app.py`：NoneBot 事件接入与消息路由
- `config/`：YAML 配置（主配置、人设、触发词、敏感词）
- `core/`：核心引擎（Thinking、Trigger、Memory、Search、Image、Markdown、Personality）
- `services/`：外部服务封装（SkiAPI、日志）
- `plugins/`：插件目录（示例插件：`example_plugin.py`）
- `storage/`：本地运行数据（日志、记忆）
- `utils/`：通用工具函数

## 快速启动

```powershell
cd d:\Project\YuKiKo\yukiko-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

## 配置项说明

主配置文件：`config/config.yml`

- `bot`：机器人名称、昵称、语言与功能开关
- `api`：模型提供方、地址、模型名、温度、超时
- `memory`：长期记忆与摘要策略
- `trigger`：触发策略、随机回复概率、会话超时
- `search`：联网搜索开关与结果数量
- `image`：图片生成开关与尺寸
- `markdown`：回复渲染开关
- `limits`：调用额度开关与上限

环境变量文件：`.env`

- `HOST`、`PORT`：NoneBot 监听地址
- `ONEBOT_ACCESS_TOKEN`：必须与 NapCat OneBot11 token 一致
- `SKIAPI_KEY`：大模型调用密钥

## NapCat 对接步骤

1. 启动 NapCat 并完成 QQ 登录。
2. 在 NapCat 的 OneBot11 网络配置中启用反向 WebSocket。
3. 反向地址填：`ws://127.0.0.1:8080/onebot/v11/ws`
4. token 填：`.env` 中的 `ONEBOT_ACCESS_TOKEN`
5. 启动 `python main.py`，日志出现 `Bot xxx connected` 即代表链路成功。

## 触发与会话机制

满足以下任一条件时，机器人进入处理流程：

1. 被 `@`
2. 消息中提到昵称（如：雪、yukiko）
3. 命中触发关键词
4. 命中敏感词分析
5. 命中随机触发概率
6. 当前会话仍在 `active_session` 有效期内

触发后流程为：

收到消息 -> 触发判断 -> 记忆检索 -> 决策（回复/搜索/生图/忽略） -> 输出回复 -> 写入记忆

## 插件扩展

插件放在 `plugins/` 目录，最小结构如下：

```python
class Plugin:
    name = "demo"
    commands = ["/demo"]

    async def handle(self, message: str, context: dict) -> str:
        return "示例返回"
```

机器人启动时会自动扫描并加载插件。

## 常见问题与排错

1. 没有回复
- 检查是否满足触发条件（是否 `@`、是否命中关键词、会话是否激活）
- 检查 NoneBot 日志是否收到消息事件

2. 提示连接拒绝 `ECONNREFUSED 127.0.0.1:8080`
- 先启动 `python main.py`，再启动 NapCat，或等待 NapCat 自动重连

3. 一直是降级回复
- 检查 `.env` 是否填写了 `SKIAPI_KEY`
- 检查 `config/config.yml` 的 `api.base_url` 是否为 `https://skiapi.dev/v1`

4. token 鉴权失败
- 确认 NapCat 与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 完全一致
