import { useEffect, useMemo, useState, useCallback, useRef } from "react";
import {
  Card, CardBody, CardHeader, Input, Switch, Button, Select, SelectItem, Textarea,
  Spinner, Slider, Chip, Tabs, Tab,
} from "@heroui/react";
import { Save, Undo2 } from "lucide-react";
import { motion } from "framer-motion";
import { api, type EnvSettingEntry, type EnvUpdateResponse, type ImageGenTestResponse } from "../../api/client";
import { ModelCombobox } from "../../components/model-combobox";
import { NotificationContainer } from "../../components/notification";
import { useNotifications } from "../../hooks/useNotifications";
import type { Cfg, FieldDef, EnvDraftMap } from "./config-schema";
import {
  SECTIONS, SECTION_META, MODEL_OPTIONS, IMAGE_MODEL_OPTIONS, IMAGE_GEN_PROMPT_PRESETS,
  allModelOptions, uniqueModelOptions,
  INPUT_CLASSES, SELECT_CLASSES, SHELL,
} from "./config-schema";
import {
  getPath, setPath, parseListValue,
  parseGroupVerbosityMap, formatGroupVerbosityMap,
  parseGroupTextMap, formatGroupTextMap,
  parseTextMap, formatTextMap,
  parseNumberInput, withDefaults,
} from "./config-helpers";

export default function ConfigPage() {
  const { notifications, success, danger } = useNotifications();
  const undoSnapshotRef = useRef<Cfg | null>(null);
  const [config, setConfig] = useState<Cfg>({});
  const [envFile, setEnvFile] = useState("");
  const [envEntries, setEnvEntries] = useState<EnvSettingEntry[]>([]);
  const [envDrafts, setEnvDrafts] = useState<EnvDraftMap>({});
  const [envSaving, setEnvSaving] = useState(false);
  const [envSaveResult, setEnvSaveResult] = useState<EnvUpdateResponse | null>(null);
  const [envPanelOpen, setEnvPanelOpen] = useState(false);
  const [fieldDrafts, setFieldDrafts] = useState<Record<string, string>>({});
  const [numberDrafts, setNumberDrafts] = useState<Record<string, string>>({});
  const [listDrafts, setListDrafts] = useState<Record<string, string>>({});
  const [rawConfigText, setRawConfigText] = useState("");
  const [rawConfigError, setRawConfigError] = useState("");
  const [rawConfigDirty, setRawConfigDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [activeSection, setActiveSection] = useState("control");
  const [compactMode, setCompactMode] = useState(true);
  const [advancedOpenSections, setAdvancedOpenSections] = useState<Record<string, boolean>>({});
  const [jsonMode, setJsonMode] = useState<"sections" | "raw">("sections");
  const [jsonPanelOpen, setJsonPanelOpen] = useState(false);
  const [jsonSectionKey, setJsonSectionKey] = useState("control");
  const [jsonSectionText, setJsonSectionText] = useState("");
  const [imageGenTestPrompt, setImageGenTestPrompt] = useState("一只可爱的猫娘女仆，二次元插画，精致细节");
  const [imageGenTestModel, setImageGenTestModel] = useState("");
  const [imageGenTestSize, setImageGenTestSize] = useState("");
  const [imageGenTestStyle, setImageGenTestStyle] = useState("");
  const [imageGenTesting, setImageGenTesting] = useState(false);
  const [imageGenTestResult, setImageGenTestResult] = useState<ImageGenTestResponse | null>(null);
  const applyImageGenPreset = (prompt: string) => {
    setImageGenTestPrompt(prompt);
  };

  const activeIndex = useMemo(() => Math.max(0, SECTIONS.findIndex((s) => s.key === activeSection)), [activeSection]);
  const active = SECTIONS[activeIndex];
  const activeMeta = SECTION_META[active?.key || ""] || { description: "这里是当前配置分区。", essentials: [] };
  const activeEssentialPaths = useMemo(() => {
    if (!active) return new Set<string>();
    const essentials = activeMeta.essentials || [];
    if (essentials.length > 0) return new Set(essentials);
    return new Set(active.fields.slice(0, Math.min(active.fields.length, 4)).map((field) => field.path));
  }, [active, activeMeta]);
  const activeEssentialFields = useMemo(
    () => active.fields.filter((field) => !compactMode || activeEssentialPaths.has(field.path)),
    [active, compactMode, activeEssentialPaths],
  );
  const activeAdvancedFields = useMemo(
    () => compactMode ? active.fields.filter((field) => !activeEssentialPaths.has(field.path)) : [],
    [active, compactMode, activeEssentialPaths],
  );
  const activeAdvancedOpen = !!advancedOpenSections[active.key];
  const mainModelOptions = useMemo(() => {
    const providerValue = String(getPath(config, "api.provider") ?? "");
    return uniqueModelOptions(MODEL_OPTIONS[providerValue], allModelOptions(MODEL_OPTIONS));
  }, [config]);
  const imageModelOptions = useMemo(() => {
    const configured = getPath(config, "image_gen.models");
    const configuredOptions = Array.isArray(configured)
      ? configured.flatMap((item) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) return [];
        const data = item as Record<string, unknown>;
        const model = String(data.model ?? "").trim();
        const name = String(data.name ?? "").trim();
        const provider = String(data.provider ?? "").trim();
        const value = model || name;
        if (!value) return [];
        return [{ value, label: name || value, description: provider ? `${provider} 已配置` : "已配置" }];
      })
      : [];
    const provider = String(
      Array.isArray(configured)
        ? ((configured.find((item) => item && typeof item === "object" && !Array.isArray(item)) as Record<string, unknown> | undefined)?.provider ?? "")
        : "",
    ).trim().toLowerCase();
    return uniqueModelOptions(configuredOptions, IMAGE_MODEL_OPTIONS[provider], allModelOptions(IMAGE_MODEL_OPTIONS));
  }, [config]);
  const topLevelJsonKeys = useMemo(() => {
    return Object.keys(config).filter((k) => typeof k === "string" && k.trim()).sort();
  }, [config]);

  const syncEnvEntries = useCallback((entries: EnvSettingEntry[]) => {
    setEnvEntries(entries);
    setEnvDrafts(Object.fromEntries(entries.map((entry) => [entry.key, String(entry.value ?? "")])));
  }, []);

  const load = useCallback(async () => {
    try {
      const [configRes, envRes] = await Promise.all([api.getConfig(), api.getEnvSettings()]);
      const merged = withDefaults((configRes.config || {}) as Cfg);
      setConfig(merged);
      setFieldDrafts({});
      setNumberDrafts({});
      setListDrafts({});
      setRawConfigText(JSON.stringify(merged, null, 2));
      setRawConfigError("");
      setRawConfigDirty(false);
      setEnvFile(envRes.env_file || "");
      syncEnvEntries(envRes.entries || []);
      setEnvSaveResult(null);
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [syncEnvEntries]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (topLevelJsonKeys.length === 0) return;
    const key = topLevelJsonKeys.includes(jsonSectionKey) ? jsonSectionKey : topLevelJsonKeys[0];
    if (key !== jsonSectionKey) {
      setJsonSectionKey(key);
      return;
    }
    const sectionValue = getPath(config, key);
    setJsonSectionText(JSON.stringify(sectionValue, null, 2));
  }, [config, jsonSectionKey, topLevelJsonKeys]);

  useEffect(() => {
    if (!imageGenTestModel) {
      const fallbackModel = String(getPath(config, "image_gen.default_model") ?? "").trim();
      if (fallbackModel) setImageGenTestModel(fallbackModel);
    }
    if (!imageGenTestSize) {
      const fallbackSize = String(getPath(config, "image_gen.default_size") ?? "").trim();
      if (fallbackSize) setImageGenTestSize(fallbackSize);
    }
  }, [config, imageGenTestModel, imageGenTestSize]);

  const updateField = (path: string, value: unknown) => setConfig((prev) => {
    const next = setPath(prev, path, value);
    setFieldDrafts((drafts) => {
      if (!(path in drafts)) return drafts;
      const copied = { ...drafts };
      delete copied[path];
      return copied;
    });
    setNumberDrafts((drafts) => {
      if (!(path in drafts)) return drafts;
      const copied = { ...drafts };
      delete copied[path];
      return copied;
    });
    setListDrafts((drafts) => {
      if (!(path in drafts)) return drafts;
      const copied = { ...drafts };
      delete copied[path];
      return copied;
    });
    setRawConfigText(JSON.stringify(next, null, 2));
    setRawConfigError("");
    setRawConfigDirty(false);
    return next;
  });

  const commitMapDraft = useCallback((field: FieldDef, raw: string) => {
    if (field.type === "group_verbosity_map") {
      updateField(field.path, parseGroupVerbosityMap(raw));
      return;
    }
    if (field.type === "group_text_map") {
      updateField(field.path, parseGroupTextMap(raw));
      return;
    }
    if (field.type === "text_map") {
      updateField(field.path, parseTextMap(raw));
    }
  }, []);

  const commitNumberDraft = useCallback((field: FieldDef, raw: string, current: unknown) => {
    updateField(field.path, parseNumberInput(raw, current, field));
  }, []);

  const applyPendingDrafts = useCallback((base: Cfg): Cfg => {
    let next = base;
    const fieldMap = new Map<string, FieldDef>();
    for (const section of SECTIONS) {
      for (const field of section.fields) fieldMap.set(field.path, field);
    }
    for (const [path, raw] of Object.entries(fieldDrafts)) {
      const field = fieldMap.get(path);
      if (!field) continue;
      if (field.type === "group_verbosity_map") {
        next = setPath(next, path, parseGroupVerbosityMap(raw));
      } else if (field.type === "group_text_map") {
        next = setPath(next, path, parseGroupTextMap(raw));
      } else if (field.type === "text_map") {
        next = setPath(next, path, parseTextMap(raw));
      }
    }
    for (const [path, raw] of Object.entries(numberDrafts)) {
      const field = fieldMap.get(path);
      if (!field || field.type !== "number") continue;
      const current = getPath(next, path);
      next = setPath(next, path, parseNumberInput(raw, current, field));
    }
    for (const [path, raw] of Object.entries(listDrafts)) {
      const field = fieldMap.get(path);
      if (!field || field.type !== "list") continue;
      next = setPath(next, path, parseListValue(raw));
    }
    return next;
  }, [fieldDrafts, listDrafts, numberDrafts]);

  const resolveConfigForAction = useCallback((): Cfg => {
    let payload = applyPendingDrafts(config);
    if (rawConfigDirty) {
      const parsed = JSON.parse(rawConfigText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("根节点必须是 JSON 对象");
      }
      payload = withDefaults(parsed as Cfg);
    }
    return withDefaults(payload);
  }, [applyPendingDrafts, config, rawConfigDirty, rawConfigText]);

  const handleSave = async () => {
    setSaving(true);
    setMsg("");
    try {
      const payload = resolveConfigForAction();
      undoSnapshotRef.current = JSON.parse(JSON.stringify(config));
      const res = await api.updateConfig(payload);
      if (res.ok) {
        success("保存成功", "配置已保存并热重载", 4000);
        await load();
      } else {
        danger("保存失败", res.message || "未知错误", 5000);
      }
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : "未知错误";
      setRawConfigError(`JSON 解析失败: ${detail}`);
      danger("保存失败", detail, 5000);
    } finally {
      setSaving(false);
    }
  };

  const handleEnvSave = async () => {
    setEnvSaving(true);
    setEnvSaveResult(null);
    try {
      const payload = Object.fromEntries(envEntries.map((entry) => [entry.key, envDrafts[entry.key] ?? ""]));
      const res = await api.updateEnvSettings(payload);
      setEnvSaveResult(res);
      syncEnvEntries(res.entries || []);

      if (res.reauth_required) {
        success("环境变量已保存", "WEBUI_TOKEN 已更新，正在跳转到登录页", 3000);
        window.setTimeout(() => {
          api.clearToken();
          window.location.href = "/webui/login";
        }, 800);
        return;
      }

      success("环境变量已保存", res.message || "保存完成", 4000);
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : "未知错误";
      danger("环境变量保存失败", detail, 5000);
    } finally {
      setEnvSaving(false);
    }
  };

  const handleUndo = async () => {
    if (!undoSnapshotRef.current) return;
    setSaving(true);
    try {
      const res = await api.updateConfig(undoSnapshotRef.current);
      if (res.ok) {
        success("撤销成功", "已恢复到上次保存前的配置", 4000);
        undoSnapshotRef.current = null;
        await load();
      } else {
        danger("撤销失败", res.message || "未知错误", 5000);
      }
    } catch (e: unknown) {
      danger("撤销失败", e instanceof Error ? e.message : "未知错误", 5000);
    } finally {
      setSaving(false);
    }
  };

  const handleTestImageGen = async () => {
    setImageGenTesting(true);
    setImageGenTestResult(null);
    setMsg("");
    try {
      const payload = resolveConfigForAction();
      const imageGenCfg = getPath(payload, "image_gen");
      const imageGenOverride = imageGenCfg && typeof imageGenCfg === "object" && !Array.isArray(imageGenCfg)
        ? (imageGenCfg as Record<string, unknown>)
        : undefined;

      const res = await api.testImageGen({
        prompt: imageGenTestPrompt.trim() || "一只可爱的猫娘女仆，二次元插画，精致细节",
        model: imageGenTestModel.trim() || undefined,
        size: imageGenTestSize.trim() || undefined,
        style: imageGenTestStyle.trim() || undefined,
        image_gen: imageGenOverride,
      });
      setImageGenTestResult(res);
      setMsg(res.ok ? "图片生成测试成功（未保存配置）" : `图片生成测试失败: ${res.message}`);
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : "未知错误";
      setRawConfigError(`JSON 解析失败: ${detail}`);
      setMsg("图片生成测试失败");
    } finally {
      setImageGenTesting(false);
    }
  };

  const renderField = (field: FieldDef) => {
    const val = getPath(config, field.path);
    const wide = field.type === "textarea" || field.type === "group_verbosity_map" || field.type === "group_text_map" || field.type === "text_map";
    const cls = `${SHELL} ${wide ? "lg:col-span-2 2xl:col-span-3" : ""}`;
    const selected = val === undefined || val === null || String(val) === "" ? [] : [String(val)];

    const control = (() => {
      if (field.type === "switch") {
        return <div className="flex items-center justify-between gap-4"><div className="text-sm font-medium text-default-600">{field.label}</div><Switch isSelected={!!val} onValueChange={(v) => updateField(field.path, v)} /></div>;
      }
      if (field.type === "slider") {
        return <div className="space-y-3"><div className="text-sm font-medium text-default-600">{field.label}</div><Slider step={field.step || 1} minValue={field.min || 0} maxValue={field.max || 10} value={Number(val) || field.min || 0} onChange={(v) => updateField(field.path, Array.isArray(v) ? Number(v[0]) : Number(v))} /></div>;
      }
      if (field.type === "select") {
        const providerValue = String(getPath(config, "api.provider") ?? "");
        const options = field.path === "api.model" ? (MODEL_OPTIONS[providerValue] || []) : (field.options || []);
        if (field.path === "api.model") {
          return (
            <ModelCombobox
              label={field.label}
              value={String(val ?? "")}
              onValueChange={(v) => updateField(field.path, v)}
              options={mainModelOptions}
              inputClassNames={INPUT_CLASSES}
            />
          );
        }
        return (
          <Select
            label={field.label}
            labelPlacement="outside"
            selectedKeys={selected}
            onSelectionChange={(keys) => {
              const arr = Array.from(keys);
              if (arr.length <= 0) return;
              const next = String(arr[0]);
              if (field.path === "api.provider") {
                const currentModel = String(getPath(config, "api.model") ?? "");
                const previousModels = MODEL_OPTIONS[providerValue] || [];
                const shouldResetModel = !currentModel
                  || previousModels.some((item) => item.value === currentModel);
                updateField(field.path, next);
                const models = MODEL_OPTIONS[next] || [];
                if (shouldResetModel && models.length > 0) {
                  updateField("api.model", models[0].value);
                }
                return;
              }
              updateField(field.path, next);
            }}
            classNames={SELECT_CLASSES}
          >
            {options.map((o) => <SelectItem key={o.value}>{o.label}</SelectItem>)}
          </Select>
        );
      }
      if (field.type === "textarea") {
        return <Textarea label={field.label} labelPlacement="outside" minRows={field.rows || 2} maxRows={8} value={String(val ?? "")} onValueChange={(v) => updateField(field.path, v)} classNames={INPUT_CLASSES} />;
      }
      if (field.type === "password") {
        return <Input label={field.label} labelPlacement="outside" type="password" value={String(val ?? "")} onValueChange={(v) => updateField(field.path, v)} description={val === "***" ? "已加密，留空不修改" : undefined} classNames={INPUT_CLASSES} />;
      }
      if (field.path === "image_gen.default_model") {
        return (
          <ModelCombobox
            label={field.label}
            value={String(val ?? "")}
            onValueChange={(v) => updateField(field.path, v)}
            options={imageModelOptions}
            placeholder="gpt-image-1"
            inputClassNames={INPUT_CLASSES}
          />
        );
      }
      if (field.path === "image_gen.prompt_review_model" || field.path === "image_gen.post_review_model") {
        return (
          <ModelCombobox
            label={field.label}
            value={String(val ?? "")}
            onValueChange={(v) => updateField(field.path, v)}
            options={mainModelOptions}
            placeholder="留空则使用主模型"
            description="支持搜索主模型候选项；留空则走主模型"
            inputClassNames={INPUT_CLASSES}
          />
        );
      }
      if (field.type === "number") {
        const rawValue = numberDrafts[field.path];
        const inputValue = rawValue === undefined ? String(val ?? "") : rawValue;
        return (
          <Input
            label={field.label}
            labelPlacement="outside"
            type="number"
            value={inputValue}
            min={field.min}
            max={field.max}
            step={field.step}
            onValueChange={(v) => setNumberDrafts((prev) => ({ ...prev, [field.path]: v }))}
            onBlur={() => commitNumberDraft(field, inputValue, val)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                commitNumberDraft(field, inputValue, val);
              }
            }}
            classNames={INPUT_CLASSES}
          />
        );
      }
      if (field.type === "list") {
        const list = Array.isArray(val) ? val.map((x) => String(x)) : [];
        const draft = listDrafts[field.path];
        const inputValue = draft === undefined ? list.join(", ") : draft;
        return (
          <Input
            label={field.label}
            labelPlacement="outside"
            value={inputValue}
            description="支持中英文逗号或换行分割；按 Enter 或失焦后应用"
            onValueChange={(v) => setListDrafts((prev) => ({ ...prev, [field.path]: v }))}
            onBlur={() => updateField(field.path, parseListValue(inputValue))}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                updateField(field.path, parseListValue(inputValue));
              }
            }}
            classNames={INPUT_CLASSES}
          />
        );
      }
      if (field.type === "group_verbosity_map" || field.type === "group_text_map" || field.type === "text_map") {
        const formatted = field.type === "group_verbosity_map"
          ? formatGroupVerbosityMap(val)
          : field.type === "group_text_map"
            ? formatGroupTextMap(val)
            : formatTextMap(val);
        const draft = fieldDrafts[field.path];
        const textValue = draft === undefined ? formatted : draft;
        const description = field.type === "group_verbosity_map"
          ? "每行格式: 群号=verbose|medium|brief|minimal（支持中文别名：详细/中等/简洁/极简）"
          : field.type === "group_text_map"
            ? "每行格式: 群号=文本，例如 123456=very_open 或 123456=多用口语、最多两段"
            : "每行格式: 原词=替换词，例如 色情=亲密内容";
        return (
          <Textarea
            label={field.label}
            labelPlacement="outside"
            minRows={field.rows || 4}
            maxRows={12}
            value={textValue}
            onValueChange={(v) => setFieldDrafts((prev) => ({ ...prev, [field.path]: v }))}
            onBlur={() => commitMapDraft(field, fieldDrafts[field.path] ?? textValue)}
            description={description}
            classNames={INPUT_CLASSES}
          />
        );
      }
      return <Input label={field.label} labelPlacement="outside" value={String(val ?? "")} onValueChange={(v) => updateField(field.path, v)} classNames={INPUT_CLASSES} />;
    })();

    return <motion.div key={field.path} className={cls} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} whileHover={{ y: -1 }} transition={{ duration: 0.16 }}>{control}</motion.div>;
  };

  const renderEnvField = (entry: EnvSettingEntry) => {
    const value = envDrafts[entry.key] ?? "";
    const hintParts = [entry.description];
    if (entry.secret) {
      hintParts.push(entry.present ? "已配置，保持当前占位值表示不修改，清空表示删除。" : "未配置，直接填写后保存即可。");
    }
    if (entry.restart_required) {
      hintParts.push("修改后需要重启服务。");
    } else {
      hintParts.push("支持直接热更新。");
    }
    return (
      <motion.div
        key={entry.key}
        className="rounded-2xl border border-default-400/25 bg-content2/35 p-4"
        initial={{ opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        whileHover={{ y: -1 }}
        transition={{ duration: 0.16 }}
      >
        <Input
          label={entry.label}
          labelPlacement="outside"
          type={entry.secret ? "password" : "text"}
          value={value}
          onValueChange={(next) => setEnvDrafts((prev) => ({ ...prev, [entry.key]: next }))}
          description={`${entry.key} · ${hintParts.join(" ")}`}
          classNames={INPUT_CLASSES}
        />
      </motion.div>
    );
  };

  if (loading) return <div className="flex justify-center py-20"><Spinner size="lg" /></div>;

  return (
    <>
      <NotificationContainer notifications={notifications} />
      <div className="space-y-4 max-w-none">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2"><h2 className="text-xl font-bold">配置编辑</h2><Chip size="sm" variant="flat" color="primary">{active.label}</Chip></div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-2 rounded-full border border-default-300/40 bg-content1/60 px-3 py-1.5">
            <span className="text-xs text-default-500">精简视图</span>
            <Switch size="sm" isSelected={compactMode} onValueChange={setCompactMode} />
          </div>
          {undoSnapshotRef.current && (
            <Button variant="flat" startContent={<Undo2 size={16} />} onPress={handleUndo} isLoading={saving}>撤销</Button>
          )}
          <Button color="primary" startContent={<Save size={16} />} isLoading={saving} onPress={handleSave}>保存并重载</Button>
        </div>
      </div>
      {msg && <p className={msg.includes("成功") ? "text-success" : "text-danger"}>{msg}</p>}

      <div className="sticky top-0 z-20 rounded-xl border border-default-400/35 bg-background/85 backdrop-blur-md p-2">
        <div className="flex flex-wrap items-center gap-2">
          {SECTIONS.map((section) => <Button key={section.key} size="sm" radius="full" variant={activeSection === section.key ? "solid" : "flat"} color={activeSection === section.key ? "primary" : "default"} onPress={() => setActiveSection(section.key)}>{section.label}</Button>)}
        </div>
      </div>

      <Card className="border border-default-400/35 bg-content1/40 backdrop-blur-md">
        <CardHeader className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <div className="font-semibold">{active.label}</div>
            <p className="text-sm text-default-500">{activeMeta.description}</p>
            <div className="flex flex-wrap items-center gap-2">
              <Chip size="sm" variant="flat" color="primary">常用 {activeEssentialFields.length}</Chip>
              {compactMode && activeAdvancedFields.length > 0 && (
                <Chip size="sm" variant="flat" color="warning">高级 {activeAdvancedFields.length}</Chip>
              )}
            </div>
          </div>
          {compactMode && activeAdvancedFields.length > 0 && (
            <Button
              variant="flat"
              onPress={() => setAdvancedOpenSections((prev) => ({ ...prev, [active.key]: !prev[active.key] }))}
            >
              {activeAdvancedOpen ? "收起高级参数" : "展开高级参数"}
            </Button>
          )}
        </CardHeader>
        <CardBody>
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">{activeEssentialFields.map(renderField)}</div>
          {compactMode && activeAdvancedFields.length > 0 && activeAdvancedOpen && (
            <div className="mt-4 space-y-3 rounded-2xl border border-warning/20 bg-warning/5 p-4">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <div className="font-medium">高级参数</div>
                  <p className="text-xs text-default-500">低频兼容项、细粒度阈值和实验性开关都放在这里。</p>
                </div>
                <Chip size="sm" variant="flat" color="warning">{activeAdvancedFields.length} 项</Chip>
              </div>
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">{activeAdvancedFields.map(renderField)}</div>
            </div>
          )}
        </CardBody>
      </Card>

      <Card className="border border-default-400/35 bg-content1/40 backdrop-blur-md">
        <CardHeader className="flex flex-wrap items-center justify-between gap-3">
          <div className="space-y-1">
            <div className="font-semibold">环境变量与 NapCat 连接</div>
            <div className="text-xs text-default-500">{envFile || ".env"}</div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {envSaveResult?.restart_required && <Chip size="sm" variant="flat" color="warning">需要重启</Chip>}
            {envSaveResult?.reauth_required && <Chip size="sm" variant="flat" color="danger">需要重新登录</Chip>}
            {envSaveResult && !envSaveResult.restart_required && !envSaveResult.reauth_required && (
              <Chip size="sm" variant="flat" color="success">已热更新</Chip>
            )}
            <Button variant="flat" onPress={() => setEnvPanelOpen((prev) => !prev)}>
              {envPanelOpen ? "收起环境变量" : "展开环境变量"}
            </Button>
            {envPanelOpen && (
              <Button color="secondary" startContent={<Save size={16} />} isLoading={envSaving} onPress={handleEnvSave}>
                保存 .env
              </Button>
            )}
          </div>
        </CardHeader>
        <CardBody className="space-y-4">
          {!envPanelOpen ? (
            <p className="text-sm text-default-500">
              环境变量编辑默认折叠，避免影响配置分区切换体验。点击「展开环境变量」即可编辑并热更新。
            </p>
          ) : (
            <>
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                {envEntries.map(renderEnvField)}
              </div>
              {envSaveResult && (
                <div className="rounded-2xl border border-default-400/25 bg-content2/35 p-4 text-sm text-default-600">
                  <div>{envSaveResult.message}</div>
                  {envSaveResult.changed_keys.length > 0 && (
                    <div className="mt-2 text-xs text-default-500">已更新: {envSaveResult.changed_keys.join(", ")}</div>
                  )}
                  {envSaveResult.reload_message && (
                    <div className="mt-2 text-xs text-default-500">{envSaveResult.reload_message}</div>
                  )}
                </div>
              )}
            </>
          )}
        </CardBody>
      </Card>

      {active.key === "image_gen" && (
        <Card className="border border-default-400/35 bg-content1/35 backdrop-blur-sm">
          <CardHeader className="flex items-center justify-between gap-2">
            <div className="font-semibold">测试生成（不保存配置）</div>
            <Chip size="sm" variant="flat" color="primary">开箱即用验证</Chip>
          </CardHeader>
          <CardBody className="space-y-3">
            <p className="text-xs text-default-500">
              会使用你当前页面里的配置（包含未保存修改）进行一次真实图片生成测试。
            </p>
            <div className="space-y-2">
              <div className="text-xs text-default-500">提示词模板（点击快速填充）</div>
              <div className="flex flex-wrap items-center gap-2">
                {IMAGE_GEN_PROMPT_PRESETS.map((preset) => (
                  <Chip
                    key={preset.label}
                    variant="flat"
                    color="default"
                    className="cursor-pointer"
                    onClick={() => applyImageGenPreset(preset.prompt)}
                  >
                    {preset.label}
                  </Chip>
                ))}
              </div>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <Textarea
                label="测试提示词"
                labelPlacement="outside"
                minRows={3}
                maxRows={6}
                value={imageGenTestPrompt}
                onValueChange={setImageGenTestPrompt}
                classNames={INPUT_CLASSES}
              />
              <div className="space-y-3">
                <ModelCombobox
                  label="测试模型（可留空=走默认模型）"
                  value={imageGenTestModel}
                  onValueChange={setImageGenTestModel}
                  options={imageModelOptions}
                  placeholder="留空则走默认模型"
                  description="支持搜索图片模型候选项，也可以直接输入网关模型名"
                  inputClassNames={INPUT_CLASSES}
                />
                <Select
                  label="测试尺寸"
                  labelPlacement="outside"
                  selectedKeys={imageGenTestSize ? [imageGenTestSize] : []}
                  onSelectionChange={(keys) => {
                    const arr = Array.from(keys);
                    setImageGenTestSize(arr.length > 0 ? String(arr[0]) : "");
                  }}
                  classNames={SELECT_CLASSES}
                >
                  {["1024x1024", "1792x1024", "1024x1792"].map((size) => (
                    <SelectItem key={size}>{size}</SelectItem>
                  ))}
                </Select>
                <Input
                  label="测试风格（可选）"
                  labelPlacement="outside"
                  value={imageGenTestStyle}
                  onValueChange={setImageGenTestStyle}
                  classNames={INPUT_CLASSES}
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button color="primary" isLoading={imageGenTesting} onPress={handleTestImageGen}>
                开始测试生成
              </Button>
            </div>

            {imageGenTestResult && (
              <div className="rounded-xl border border-default-400/35 bg-content2/35 p-3 space-y-2">
                <p className={imageGenTestResult.ok ? "text-success text-sm" : "text-danger text-sm"}>
                  {imageGenTestResult.message}
                </p>
                <div className="text-xs text-default-500 flex flex-wrap gap-3">
                  <span>请求模型: {imageGenTestResult.requested_model || "(默认)"}</span>
                  <span>实际模型: {imageGenTestResult.model_used || "-"}</span>
                  <span>默认模型: {imageGenTestResult.default_model || "-"}</span>
                  <span>已配模型数: {Number(imageGenTestResult.configured_models ?? 0)}</span>
                </div>
                {imageGenTestResult.revised_prompt && (
                  <p className="text-xs text-default-500 break-all">
                    revised_prompt: {imageGenTestResult.revised_prompt}
                  </p>
                )}
                {imageGenTestResult.image_url && (
                  <div className="pt-1">
                    <img
                      src={imageGenTestResult.image_url}
                      alt="image-gen-test"
                      className="max-h-80 rounded-lg border border-default-300/40"
                    />
                  </div>
                )}
              </div>
            )}
          </CardBody>
        </Card>
      )}

      <Card className="border border-default-400/35 bg-content1/35 backdrop-blur-sm">
        <CardHeader className="flex flex-wrap items-center justify-between gap-2">
          <div className="space-y-1">
            <div className="font-semibold">JSON编辑区</div>
            <p className="text-xs text-default-500">普通调整建议优先用上面的表单；这里保留给高级排查和整段复制。</p>
          </div>
          <div className="flex items-center gap-2">
            {jsonPanelOpen && (
              <Tabs
                selectedKey={jsonMode}
                onSelectionChange={(key) => setJsonMode(String(key) as "sections" | "raw")}
                size="sm"
                color="primary"
                variant="bordered"
                className="max-w-full"
              >
                <Tab key="sections" title="结构浏览/片段编辑" />
                <Tab key="raw" title="全量原始 JSON" />
              </Tabs>
            )}
            <Button variant="flat" onPress={() => setJsonPanelOpen((prev) => !prev)}>
              {jsonPanelOpen ? "收起 JSON 编辑区" : "展开 JSON 编辑区"}
            </Button>
          </div>
        </CardHeader>
        <CardBody>
          {!jsonPanelOpen ? (
            <p className="text-sm text-default-500">
              JSON 编辑区默认折叠，避免和常用参数表单混在一起。需要排查底层配置时再展开即可。
            </p>
          ) : jsonMode === "sections" ? (
            <div className="grid grid-cols-1 lg:grid-cols-[240px_1fr] gap-3">
              <div className="rounded-xl border border-default-400/30 bg-content2/40 p-2 max-h-[420px] overflow-auto">
                <div className="text-xs text-default-500 px-2 py-1">顶级 JSON 段</div>
                <div className="flex flex-col gap-1">
                  {topLevelJsonKeys.map((key) => (
                    <Button
                      key={key}
                      size="sm"
                      variant={jsonSectionKey === key ? "flat" : "light"}
                      color={jsonSectionKey === key ? "primary" : "default"}
                      className="justify-start"
                      onPress={() => setJsonSectionKey(key)}
                    >
                      {key}
                    </Button>
                  ))}
                </div>
              </div>
              <div className="space-y-3">
                <Textarea
                  label={`JSON 片段：${jsonSectionKey}`}
                  labelPlacement="outside"
                  minRows={12}
                  maxRows={22}
                  value={jsonSectionText}
                  isReadOnly
                  description="仅用于查看当前片段结构"
                  classNames={INPUT_CLASSES}
                />
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              <Textarea
                label="全量配置 JSON（可编辑）"
                labelPlacement="outside"
                minRows={12}
                maxRows={30}
                value={rawConfigText}
                onValueChange={(v) => { setRawConfigText(v); setRawConfigDirty(true); }}
                description={rawConfigError || (rawConfigDirty ? "有未应用的 JSON 修改，保存时会优先用这里的内容" : undefined)}
                color={rawConfigError ? "danger" : "default"}
                classNames={INPUT_CLASSES}
              />
            </div>
          )}
        </CardBody>
      </Card>
    </div>
    </>
  );
}
