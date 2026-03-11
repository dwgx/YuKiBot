import { useEffect, useState } from "react";
import { Card, CardBody, CardHeader, Button, Switch, Textarea, Input, Chip, Spinner, Accordion, AccordionItem } from "@heroui/react";
import { Save, RefreshCw, Settings } from "lucide-react";
import { api } from "../api/client";

interface PluginConfig {
  [key: string]: unknown;
}

interface Plugin {
  name: string;
  description: string;
  enabled: boolean;
  config: PluginConfig;
  args_schema: {
    type: string;
    properties?: Record<string, {
      type: string;
      description?: string;
      default?: unknown;
      enum?: unknown[];
    }>;
  };
  config_schema?: {
    type: string;
    properties?: Record<string, {
      type: string;
      description?: string;
      default?: unknown;
      enum?: unknown[];
      minimum?: number;
      maximum?: number;
    }>;
  };
  agent_tool: boolean;
  internal_only: boolean;
}

export default function PluginsPage() {
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [msg, setMsg] = useState("");

  const loadPlugins = async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/webui/plugins", {
        headers: { Authorization: `Bearer ${api.getToken()}` },
      });
      const data = await res.json();
      setPlugins(data.plugins || []);
    } catch (err: any) {
      setMsg(`加载失败: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadPlugins();
  }, []);

  const handleSave = async (pluginName: string, config: PluginConfig, enabled: boolean) => {
    setSaving(pluginName);
    setMsg("");
    try {
      const res = await fetch(`/api/webui/plugins/${pluginName}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${api.getToken()}`,
        },
        body: JSON.stringify({ config, enabled, reload: true }),
      });
      if (res.ok) {
        setMsg(`${pluginName} 保存成功`);
        await loadPlugins();
      } else {
        setMsg(`${pluginName} 保存失败: ${res.statusText}`);
      }
    } catch (err: any) {
      setMsg(`错误: ${err.message}`);
    } finally {
      setSaving(null);
    }
  };

  const renderConfigField = (
    plugin: Plugin,
    key: string,
    schema: { type: string; description?: string; default?: unknown; enum?: unknown[]; minimum?: number; maximum?: number }
  ) => {
    const value = plugin.config[key];
    const label = schema.description || key;

    const updateConfig = (newValue: unknown) => {
      const newConfig = { ...plugin.config, [key]: newValue };
      setPlugins(plugins.map(p => p.name === plugin.name ? { ...p, config: newConfig } : p));
    };

    if (schema.type === "boolean") {
      return (
        <div key={key} className="flex items-center justify-between py-2">
          <span className="text-sm">{label}</span>
          <Switch
            isSelected={Boolean(value)}
            onValueChange={updateConfig}
          />
        </div>
      );
    }

    if (schema.type === "number" || schema.type === "integer") {
      return (
        <Input
          key={key}
          label={label}
          type="number"
          value={String(value ?? schema.default ?? "")}
          onValueChange={(v) => updateConfig(Number(v) || 0)}
          size="sm"
        />
      );
    }

    if (schema.enum && Array.isArray(schema.enum)) {
      return (
        <div key={key} className="space-y-1">
          <label className="text-sm">{label}</label>
          <select
            className="w-full px-3 py-2 rounded-lg bg-default-100 text-sm"
            value={String(value ?? schema.default ?? "")}
            onChange={(e) => updateConfig(e.target.value)}
          >
            {schema.enum.map((opt) => (
              <option key={String(opt)} value={String(opt)}>
                {String(opt)}
              </option>
            ))}
          </select>
        </div>
      );
    }

    if (schema.type === "array") {
      const arrayValue = Array.isArray(value) ? value : (schema.default as any[]) || [];
      return (
        <Textarea
          key={key}
          label={label}
          value={arrayValue.join(", ")}
          onValueChange={(v) => {
            const items = v.split(",").map(s => s.trim()).filter(Boolean);
            updateConfig(items);
          }}
          minRows={2}
          maxRows={4}
          size="sm"
          description="用逗号分隔多个值"
        />
      );
    }

    return (
      <Textarea
        key={key}
        label={label}
        value={String(value ?? schema.default ?? "")}
        onValueChange={updateConfig}
        minRows={2}
        maxRows={6}
        size="sm"
      />
    );
  };

  const groupConfigFields = (properties: Record<string, any>) => {
    const groups: Record<string, Array<[string, any]>> = {};

    Object.entries(properties).forEach(([key, schema]) => {
      const parts = key.split(".");
      const groupName = parts.length > 1 ? parts[0] : "基础设置";

      if (!groups[groupName]) {
        groups[groupName] = [];
      }
      groups[groupName].push([key, schema]);
    });

    return groups;
  };

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner size="lg" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">插件管理</h2>
        <Button
          variant="flat"
          startContent={<RefreshCw size={16} />}
          onPress={loadPlugins}
        >
          刷新
        </Button>
      </div>

      {msg && (
        <p className={msg.includes("成功") ? "text-success" : "text-danger"}>
          {msg}
        </p>
      )}

      <Accordion selectionMode="multiple" variant="splitted">
        {plugins.map((plugin) => (
          <AccordionItem
            key={plugin.name}
            title={
              <div className="flex items-center gap-2">
                <Settings size={18} />
                <span className="font-semibold">{plugin.name}</span>
                {plugin.agent_tool && <Chip size="sm" color="primary" variant="flat">Agent工具</Chip>}
                {plugin.internal_only && <Chip size="sm" color="warning" variant="flat">内部</Chip>}
                <Chip size="sm" color={plugin.enabled ? "success" : "default"} variant="flat">
                  {plugin.enabled ? "已启用" : "已禁用"}
                </Chip>
              </div>
            }
            subtitle={plugin.description}
          >
            <Card className="border border-default-200">
              <CardHeader>
                <div className="flex items-center justify-between w-full">
                  <span className="text-sm font-medium">插件配置</span>
                  <Switch
                    isSelected={plugin.enabled}
                    onValueChange={(enabled) => {
                      setPlugins(plugins.map(p => p.name === plugin.name ? { ...p, enabled } : p));
                    }}
                  >
                    启用插件
                  </Switch>
                </div>
              </CardHeader>
              <CardBody className="space-y-3">
                {plugin.config_schema?.properties ? (
                  <>
                    {Object.entries(groupConfigFields(plugin.config_schema.properties)).map(([groupName, fields]) => (
                      <div key={groupName} className="space-y-2">
                        <div className="text-xs font-semibold text-primary uppercase tracking-wide border-b border-default-200 pb-1">
                          {groupName}
                        </div>
                        <div className="space-y-3 pl-2">
                          {fields.map(([key, schema]) => renderConfigField(plugin, key, schema))}
                        </div>
                      </div>
                    ))}
                  </>
                ) : plugin.args_schema?.properties ? (
                  Object.entries(plugin.args_schema.properties).map(([key, schema]) =>
                    renderConfigField(plugin, key, schema)
                  )
                ) : Object.keys(plugin.config).length > 0 ? (
                  Object.entries(plugin.config).map(([key, value]) => (
                    <Textarea
                      key={key}
                      label={key}
                      value={typeof value === "object" ? JSON.stringify(value, null, 2) : String(value ?? "")}
                      onValueChange={(v) => {
                        const newConfig = { ...plugin.config };
                        try {
                          newConfig[key] = JSON.parse(v);
                        } catch {
                          newConfig[key] = v;
                        }
                        setPlugins(plugins.map(p => p.name === plugin.name ? { ...p, config: newConfig } : p));
                      }}
                      minRows={2}
                      maxRows={8}
                      size="sm"
                    />
                  ))
                ) : (
                  <p className="text-sm text-default-500">此插件无可配置项</p>
                )}

                <div className="flex justify-end pt-2">
                  <Button
                    color="primary"
                    startContent={<Save size={16} />}
                    isLoading={saving === plugin.name}
                    onPress={() => handleSave(plugin.name, plugin.config, plugin.enabled)}
                  >
                    保存配置
                  </Button>
                </div>
              </CardBody>
            </Card>
          </AccordionItem>
        ))}
      </Accordion>
    </div>
  );
}
