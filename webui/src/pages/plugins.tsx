import { useDeferredValue, useEffect, useMemo, useState } from "react";
import {
  Accordion,
  AccordionItem,
  Button,
  Card,
  CardBody,
  CardHeader,
  Chip,
  Input,
  Spinner,
  Switch,
  Textarea,
} from "@heroui/react";
import { Blocks, RefreshCw, Save, Search, Sparkles, Wrench } from "lucide-react";
import { api } from "../api/client";

interface PluginConfig {
  [key: string]: unknown;
}

interface FieldSchema {
  type: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
}

interface PluginSchema {
  type?: string;
  properties?: Record<string, FieldSchema>;
}

interface Plugin {
  name: string;
  description: string;
  enabled: boolean;
  source: string;
  config_file: string;
  config_target: string;
  config_guide: string[];
  editable_keys: string[];
  configurable: boolean;
  supports_interactive_setup: boolean;
  using_defaults: boolean;
  setup_mode: string;
  config: PluginConfig;
  args_schema: PluginSchema;
  config_schema?: PluginSchema;
  agent_tool: boolean;
  internal_only: boolean;
}

type FieldGroup = {
  name: string;
  items: Array<{ key: string; schema: FieldSchema }>;
};

function humanizeFieldName(key: string): string {
  const leaf = key.split(".").pop() || key;
  return leaf
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatValuePreview(value: unknown): string {
  if (typeof value === "boolean") return value ? "启用" : "关闭";
  if (Array.isArray(value)) return value.join(", ");
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value ?? "");
}

function getFieldGroups(properties: Record<string, FieldSchema>): FieldGroup[] {
  const groups = new Map<string, Array<{ key: string; schema: FieldSchema }>>();
  Object.entries(properties).forEach(([key, schema]) => {
    const groupName = key.includes(".") ? key.split(".")[0] : "基础";
    const bucket = groups.get(groupName) || [];
    bucket.push({ key, schema });
    groups.set(groupName, bucket);
  });
  return [...groups.entries()].map(([name, items]) => ({ name, items }));
}

function parseTextareaValue(schema: FieldSchema, value: string): unknown {
  if (schema.type === "array") {
    return value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }
  if (schema.type === "integer" || schema.type === "number") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return value;
}

export default function PluginsPage() {
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [rawConfigDrafts, setRawConfigDrafts] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [keyword, setKeyword] = useState("");
  const deferredKeyword = useDeferredValue(keyword);

  const loadPlugins = async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/webui/plugins", {
        headers: { Authorization: `Bearer ${api.getToken()}` },
      });
      const data = await res.json();
      const nextPlugins = Array.isArray(data.plugins) ? data.plugins : [];
      setPlugins(nextPlugins);
      setRawConfigDrafts(
        Object.fromEntries(
          nextPlugins.map((plugin: Plugin) => [plugin.name, JSON.stringify(plugin.config, null, 2)]),
        ),
      );
      setMessage("");
    } catch (err: unknown) {
      setMessage(`加载失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadPlugins();
  }, []);

  const filteredPlugins = useMemo(() => {
    const term = deferredKeyword.trim().toLowerCase();
    if (!term) return plugins;
    return plugins.filter((plugin) => {
      const haystack = [
        plugin.name,
        plugin.description,
        plugin.config_target,
        plugin.source,
        ...(plugin.config_guide || []),
      ].join(" ").toLowerCase();
      return haystack.includes(term);
    });
  }, [deferredKeyword, plugins]);

  const updatePluginState = (pluginName: string, updater: (plugin: Plugin) => Plugin) => {
    let nextRaw = "";
    setPlugins((prev) => prev.map((plugin) => {
      if (plugin.name !== pluginName) return plugin;
      const next = updater(plugin);
      nextRaw = JSON.stringify(next.config, null, 2);
      return next;
    }));
    if (nextRaw) {
      setRawConfigDrafts((drafts) => ({
        ...drafts,
        [pluginName]: nextRaw,
      }));
    }
  };

  const updatePluginConfig = (pluginName: string, key: string, value: unknown) => {
    updatePluginState(pluginName, (plugin) => ({
      ...plugin,
      config: {
        ...plugin.config,
        [key]: value,
      },
    }));
  };

  const handleSave = async (pluginName: string, config: PluginConfig, enabled: boolean) => {
    setSaving(pluginName);
    setMessage("");
    try {
      const res = await fetch(`/api/webui/plugins/${pluginName}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${api.getToken()}`,
        },
        body: JSON.stringify({ config, enabled, reload: true }),
      });
      if (!res.ok) {
        throw new Error(await res.text());
      }
      setMessage(`${pluginName} 配置已保存并热重载`);
      await loadPlugins();
    } catch (err: unknown) {
      setMessage(`保存失败: ${err instanceof Error ? err.message : "未知错误"}`);
    } finally {
      setSaving(null);
    }
  };

  const renderFieldEditor = (plugin: Plugin, fieldKey: string, schema: FieldSchema) => {
    const currentValue = plugin.config[fieldKey] ?? schema.default ?? "";
    const description = schema.description || "未提供说明";
    const defaultValue = schema.default;

    if (schema.type === "boolean") {
      return (
        <Card key={fieldKey} className="border border-default-200/70 bg-content1/70 shadow-none">
          <CardBody className="flex flex-row items-center justify-between gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <p className="text-sm font-semibold text-default-800">{humanizeFieldName(fieldKey)}</p>
                <Chip size="sm" variant="flat">{fieldKey}</Chip>
              </div>
              <p className="mt-1 text-xs text-default-500">{description}</p>
            </div>
            <Switch
              isSelected={Boolean(currentValue)}
              onValueChange={(value) => updatePluginConfig(plugin.name, fieldKey, value)}
            />
          </CardBody>
        </Card>
      );
    }

    return (
      <Card key={fieldKey} className="border border-default-200/70 bg-content1/70 shadow-none">
        <CardBody className="space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <p className="text-sm font-semibold text-default-800">{humanizeFieldName(fieldKey)}</p>
                <Chip size="sm" variant="flat">{fieldKey}</Chip>
              </div>
              <p className="mt-1 text-xs text-default-500">{description}</p>
            </div>
            {defaultValue !== undefined && (
              <Chip size="sm" variant="flat" color="primary">
                默认: {formatValuePreview(defaultValue)}
              </Chip>
            )}
          </div>

          {schema.enum && Array.isArray(schema.enum) ? (
            <div className="space-y-2">
              <label className="text-xs font-medium text-default-500">候选值</label>
              <select
                className="w-full rounded-2xl border border-default-200 bg-content2/70 px-3 py-2 text-sm outline-none transition focus:border-primary/50"
                value={String(currentValue ?? "")}
                onChange={(evt) => updatePluginConfig(plugin.name, fieldKey, evt.target.value)}
              >
                {schema.enum.map((option) => (
                  <option key={String(option)} value={String(option)}>
                    {String(option)}
                  </option>
                ))}
              </select>
            </div>
          ) : schema.type === "integer" || schema.type === "number" ? (
            <Input
              type="number"
              value={String(currentValue ?? "")}
              onValueChange={(value) => updatePluginConfig(plugin.name, fieldKey, parseTextareaValue(schema, value))}
              description={[
                typeof schema.minimum === "number" ? `最小值 ${schema.minimum}` : "",
                typeof schema.maximum === "number" ? `最大值 ${schema.maximum}` : "",
              ].filter(Boolean).join(" · ")}
            />
          ) : (
            <Textarea
              value={Array.isArray(currentValue) ? currentValue.join(", ") : String(currentValue ?? "")}
              onValueChange={(value) => updatePluginConfig(plugin.name, fieldKey, parseTextareaValue(schema, value))}
              minRows={schema.type === "array" ? 2 : 3}
              maxRows={8}
              description={schema.type === "array" ? "数组字段使用英文逗号分隔多个值" : undefined}
            />
          )}
        </CardBody>
      </Card>
    );
  };

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner size="lg" />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <Card className="overflow-hidden border border-default-200/80 bg-[radial-gradient(circle_at_top_left,rgba(59,130,246,0.12),transparent_42%)] shadow-sm">
        <CardBody className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="inline-flex items-center gap-2 rounded-full border border-primary/20 bg-primary/10 px-3 py-1 text-xs font-medium text-primary">
              <Sparkles size={14} />
              插件配置工作台
            </div>
            <h2 className="mt-3 text-2xl font-semibold tracking-tight">把插件参数调成真正可维护的状态</h2>
            <p className="mt-2 max-w-3xl text-sm text-default-500">
              这里会同时展示插件来源、实际写回位置、配置向导提示和按分组整理后的字段。改完直接热重载，不用再猜配置应该落在哪个文件。
            </p>
          </div>
          <div className="flex w-full flex-col gap-3 sm:w-[360px]">
            <Input
              value={keyword}
              onValueChange={setKeyword}
              placeholder="搜索插件名、配置入口、说明"
              startContent={<Search size={16} className="text-default-400" />}
            />
            <Button variant="flat" startContent={<RefreshCw size={16} />} onPress={loadPlugins}>
              刷新插件清单
            </Button>
          </div>
        </CardBody>
      </Card>

      {message && (
        <Card className={`border ${message.includes("失败") ? "border-danger/30 bg-danger/10" : "border-success/30 bg-success/10"} shadow-none`}>
          <CardBody className="text-sm">{message}</CardBody>
        </Card>
      )}

      <div className="grid gap-3 sm:grid-cols-3">
        <Card className="border border-default-200/70 shadow-none">
          <CardBody className="gap-1">
            <span className="text-xs uppercase tracking-wide text-default-400">插件总数</span>
            <span className="text-2xl font-semibold">{plugins.length}</span>
          </CardBody>
        </Card>
        <Card className="border border-default-200/70 shadow-none">
          <CardBody className="gap-1">
            <span className="text-xs uppercase tracking-wide text-default-400">可配置</span>
            <span className="text-2xl font-semibold">{plugins.filter((item) => item.configurable).length}</span>
          </CardBody>
        </Card>
        <Card className="border border-default-200/70 shadow-none">
          <CardBody className="gap-1">
            <span className="text-xs uppercase tracking-wide text-default-400">当前筛选</span>
            <span className="text-2xl font-semibold">{filteredPlugins.length}</span>
          </CardBody>
        </Card>
      </div>

      <Accordion selectionMode="multiple" variant="splitted">
        {filteredPlugins.map((plugin) => {
          const fieldGroups = getFieldGroups(plugin.config_schema?.properties || {});
          const hasSchemaFields = fieldGroups.length > 0;
          const rawConfigText = rawConfigDrafts[plugin.name] ?? JSON.stringify(plugin.config, null, 2);

          return (
            <AccordionItem
              key={plugin.name}
              title={
                <div className="flex flex-wrap items-center gap-2">
                  <div className="flex h-9 w-9 items-center justify-center rounded-2xl bg-primary/10 text-primary">
                    <Blocks size={18} />
                  </div>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-default-900">{plugin.name}</span>
                      {plugin.agent_tool && <Chip size="sm" color="primary" variant="flat">Agent</Chip>}
                      {plugin.internal_only && <Chip size="sm" color="warning" variant="flat">内部</Chip>}
                      {plugin.using_defaults && <Chip size="sm" variant="flat">默认配置</Chip>}
                      <Chip size="sm" color={plugin.enabled ? "success" : "default"} variant="flat">
                        {plugin.enabled ? "已启用" : "已禁用"}
                      </Chip>
                    </div>
                    <p className="mt-1 line-clamp-2 text-xs text-default-500">{plugin.description || "暂无描述"}</p>
                  </div>
                </div>
              }
              subtitle={`配置入口: ${plugin.config_target || plugin.source || "default"}`}
            >
              <div className="space-y-4">
                <Card className="border border-default-200/80 shadow-none">
                  <CardHeader className="flex flex-col items-start gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <p className="text-sm font-semibold text-default-800">插件总览</p>
                      <p className="text-xs text-default-500">先看来源和写回位置，再决定要不要把它独立成插件专属配置文件。</p>
                    </div>
                    <Switch
                      isSelected={plugin.enabled}
                      onValueChange={(enabled) => updatePluginState(plugin.name, (prev) => ({ ...prev, enabled }))}
                    >
                      启用插件
                    </Switch>
                  </CardHeader>
                  <CardBody className="grid gap-3 lg:grid-cols-[1.25fr_1fr]">
                    <div className="grid gap-3 sm:grid-cols-2">
                      <Card className="border border-default-200/70 bg-content2/60 shadow-none">
                        <CardBody className="gap-1">
                          <span className="text-xs uppercase tracking-wide text-default-400">配置入口</span>
                          <span className="break-all text-sm font-medium text-default-800">{plugin.config_target || "default"}</span>
                        </CardBody>
                      </Card>
                      <Card className="border border-default-200/70 bg-content2/60 shadow-none">
                        <CardBody className="gap-1">
                          <span className="text-xs uppercase tracking-wide text-default-400">当前来源</span>
                          <span className="break-all text-sm font-medium text-default-800">{plugin.source || "default"}</span>
                        </CardBody>
                      </Card>
                      <Card className="border border-default-200/70 bg-content2/60 shadow-none">
                        <CardBody className="gap-1">
                          <span className="text-xs uppercase tracking-wide text-default-400">写回文件</span>
                          <span className="break-all text-sm font-medium text-default-800">{plugin.config_file || plugin.config_target || "config/plugins.yml"}</span>
                        </CardBody>
                      </Card>
                      <Card className="border border-default-200/70 bg-content2/60 shadow-none">
                        <CardBody className="gap-1">
                          <span className="text-xs uppercase tracking-wide text-default-400">配置模式</span>
                          <span className="text-sm font-medium text-default-800">{plugin.setup_mode || "manual"}</span>
                        </CardBody>
                      </Card>
                    </div>
                    <Card className="border border-default-200/70 bg-primary/5 shadow-none">
                      <CardBody className="gap-3">
                        <div className="flex items-center gap-2 text-sm font-semibold text-default-800">
                          <Wrench size={16} />
                          配置提示
                        </div>
                        {(plugin.config_guide || []).length > 0 ? (
                          <div className="space-y-2">
                            {plugin.config_guide.map((line) => (
                              <div key={line} className="rounded-2xl border border-primary/10 bg-content1/70 px-3 py-2 text-xs text-default-700">
                                {line}
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p className="text-xs text-default-500">当前插件没有额外配置提示。</p>
                        )}
                      </CardBody>
                    </Card>
                  </CardBody>
                </Card>

                {hasSchemaFields ? (
                  fieldGroups.map((group) => (
                    <Card key={group.name} className="border border-default-200/80 shadow-none">
                      <CardHeader className="flex items-center justify-between">
                        <div>
                          <p className="text-sm font-semibold text-default-800">{group.name}</p>
                          <p className="text-xs text-default-500">按配置路径分组整理，避免几十个字段混成一坨。</p>
                        </div>
                        <Chip size="sm" variant="flat">{group.items.length} 项</Chip>
                      </CardHeader>
                      <CardBody className="grid gap-3 xl:grid-cols-2">
                        {group.items.map(({ key, schema }) => renderFieldEditor(plugin, key, schema))}
                      </CardBody>
                    </Card>
                  ))
                ) : (
                  <Card className="border border-dashed border-default-300 shadow-none">
                    <CardBody className="py-8 text-center text-sm text-default-500">
                      这个插件没有提供结构化 config_schema，下面保留原始 JSON 编辑作为兜底。
                    </CardBody>
                  </Card>
                )}

                <Card className="border border-default-200/80 shadow-none">
                  <CardHeader>
                    <div>
                      <p className="text-sm font-semibold text-default-800">原始配置兜底</p>
                      <p className="text-xs text-default-500">复杂对象、临时实验字段或 schema 还没覆盖的键，可以直接从这里改。</p>
                    </div>
                  </CardHeader>
                  <CardBody className="space-y-3">
                    <Textarea
                      minRows={6}
                      maxRows={18}
                      value={rawConfigText}
                      onValueChange={(value) => {
                        setRawConfigDrafts((prev) => ({
                          ...prev,
                          [plugin.name]: value,
                        }));
                        try {
                          const parsed = JSON.parse(value);
                          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                            updatePluginState(plugin.name, (prev) => ({ ...prev, config: parsed as PluginConfig }));
                          }
                        } catch {
                          // 保持当前编辑体验，不在每次输入时打断。
                        }
                      }}
                    />
                    <div className="flex justify-end">
                      <Button
                        color="primary"
                        startContent={<Save size={16} />}
                        isLoading={saving === plugin.name}
                        onPress={() => handleSave(plugin.name, plugin.config, plugin.enabled)}
                      >
                        保存并热重载
                      </Button>
                    </div>
                  </CardBody>
                </Card>
              </div>
            </AccordionItem>
          );
        })}
      </Accordion>

      {filteredPlugins.length === 0 && (
        <Card className="border border-dashed border-default-300 shadow-none">
          <CardBody className="py-10 text-center text-default-500">
            没有匹配到插件。试试搜索插件名、配置入口，或者点一下“刷新插件清单”。
          </CardBody>
        </Card>
      )}
    </div>
  );
}
