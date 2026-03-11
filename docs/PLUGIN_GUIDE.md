# PLUGIN_GUIDE

## 简体中文

插件配置建议使用“每插件一个模板文件”：

- 位置：`plugins/config/<plugin_name>.yml`
- WebUI 插件页会读取 schema，按字段渲染配置
- 保存后可从 `/api/webui/plugins` 系列接口查看最新状态

重点模板：

- NewAPI：`plugins/config/newapi.yml`
- ConnectCLI：`plugins/config/connect_cli.yml`

详细参数说明请看：

- [zh-CN/GUIDE.md](zh-CN/GUIDE.md)

## 繁體中文

建議採用「每個插件一個 yml」：

- 路徑：`plugins/config/<plugin_name>.yml`
- 可在 WebUI 插件列表逐項管理，不必打開超大配置頁

常用模板：

- `plugins/config/newapi.yml`
- `plugins/config/connect_cli.yml`

詳細說明：

- [zh-TW/GUIDE.md](zh-TW/GUIDE.md)

## English

Use per-plugin template files:

- Path: `plugins/config/<plugin_name>.yml`
- Plugin UI can render schema-based fields from each plugin config

Common templates:

- `plugins/config/newapi.yml`
- `plugins/config/connect_cli.yml`

Details:

- [en/GUIDE.md](en/GUIDE.md)
