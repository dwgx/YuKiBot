import { useEffect, useState, useCallback, useRef } from "react";
import { Accordion, AccordionItem, Button, Input, Spinner, Switch, Textarea } from "@heroui/react";
import { Save, RefreshCw, Undo2 } from "lucide-react";
import CodeMirror from "@uiw/react-codemirror";
import { yaml } from "@codemirror/lang-yaml";
import { api } from "../api/client";
import { NotificationContainer } from "../components/notification";
import { useNotifications } from "../hooks/useNotifications";

type AnyObj = Record<string, unknown>;

type QuickField = {
  path: string;
  label: string;
  kind?: "text" | "list";
};

const QUICK_FIELDS: QuickField[] = [
  { path: "agent.identity", label: "Agent 身份定义", kind: "text" },
  { path: "agent.rules", label: "Agent 核心规则", kind: "text" },
  { path: "agent.network_flow", label: "联网处理流程", kind: "text" },
  { path: "agent.tool_usage", label: "工具使用规范", kind: "text" },
  { path: "agent.reply_style", label: "回复风格", kind: "text" },
  { path: "agent.context_rules", label: "上下文理解规则", kind: "text" },
  { path: "messages.mention_only_fallback", label: "空@回复（无名字）", kind: "text" },
  { path: "messages.mention_only_fallback_with_name", label: "空@回复（带名字）", kind: "text" },
  { path: "messages.llm_error_fallback", label: "LLM 错误回复", kind: "text" },
  { path: "messages.llm_auth_error_fallback", label: "LLM 鉴权失败回复", kind: "text" },
  { path: "messages.generic_error", label: "通用错误回复", kind: "text" },
  { path: "messages.search_followup_recent_media_title", label: "最近媒体结果标题", kind: "text" },
  { path: "messages.search_followup_recent_result_title", label: "最近搜索结果标题", kind: "text" },
  { path: "messages.explicit_fact_recall_reply", label: "事实回忆回复模板", kind: "text" },
  { path: "agent_runtime.reply_anchor_header", label: "回复锚点标题", kind: "text" },
  { path: "agent_runtime.reply_context_to_bot", label: "回复上下文（Bot）", kind: "text" },
  { path: "agent_runtime.reply_context_to_user", label: "回复上下文（用户）", kind: "text" },
  { path: "agent_runtime.attached_media_line", label: "附加媒体行", kind: "text" },
  { path: "agent_runtime.reply_media_line", label: "回复媒体行", kind: "text" },
  { path: "verbosity.verbose", label: "详细回复模板", kind: "text" },
  { path: "verbosity.medium", label: "中等回复模板", kind: "text" },
  { path: "verbosity.brief", label: "简短回复模板", kind: "text" },
  { path: "verbosity.minimal", label: "极简回复模板", kind: "text" },
];

function getNestedRawValue(obj: AnyObj, path: string): unknown {
  return path.split(".").reduce<unknown>((acc, key) => {
    if (acc && typeof acc === "object") {
      return (acc as AnyObj)[key];
    }
    return undefined;
  }, obj);
}

function getQuickFieldText(obj: AnyObj, field: QuickField): string {
  const value = getNestedRawValue(obj, field.path);
  if (field.kind === "list") {
    if (Array.isArray(value)) {
      return value
        .map((v) => String(v ?? "").trim())
        .filter(Boolean)
        .join("\n");
    }
    return "";
  }
  return typeof value === "string" ? value : "";
}

function parseQuickFieldValue(field: QuickField, text: string): unknown {
  if (field.kind === "list") {
    return text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  }
  return text;
}

function setNestedValue(obj: AnyObj, path: string, value: unknown): AnyObj {
  const keys = path.split(".");
  const root: AnyObj = { ...obj };
  let node: AnyObj = root;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i];
    const next = node[k];
    node[k] = next && typeof next === "object" ? { ...(next as AnyObj) } : {};
    node = node[k] as AnyObj;
  }
  node[keys[keys.length - 1]] = value;
  return root;
}

function asObject(value: unknown): AnyObj {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as AnyObj) : {};
}

function formatStringList(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map((item) => String(item ?? "").trim()).filter(Boolean).join("\n");
  }
  if (typeof value === "string") return value;
  return "";
}

function parseStringList(text: string): string[] {
  return text
    .split(/\r?\n|,/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export default function PromptsPage() {
  const { notifications, success, danger } = useNotifications();
  const undoContentRef = useRef<string | null>(null);
  const undoQuickRef = useRef<AnyObj | null>(null);
  const [isDark, setIsDark] = useState<boolean>(() => {
    if (typeof document === "undefined") return true;
    return document.documentElement.classList.contains("dark");
  });
  const [content, setContent] = useState("");
  const [quick, setQuick] = useState<AnyObj>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingQuick, setSavingQuick] = useState(false);
  const [savingNavigator, setSavingNavigator] = useState(false);
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await api.getPrompts();
      const text = String(res.content ?? res.yaml_text ?? "");
      setContent(text);
      setQuick(res.parsed || {});
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (typeof document === "undefined") return undefined;
    const root = document.documentElement;
    const observer = new MutationObserver(() => {
      setIsDark(root.classList.contains("dark"));
    });
    observer.observe(root, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setMsg("");
    try {
      const text = String(content ?? "");
      if (!text.trim()) {
        setMsg("提示词内容为空，先点击全量重载恢复默认模板后再保存。");
        return;
      }
      undoContentRef.current = content;
      const res = await api.updatePrompts(text);
      if (res.ok) {
        success("保存成功", "提示词已重载", 4000);
      } else {
        danger("保存失败", res.message || "未知错误", 5000);
      }
    } catch (e: unknown) {
      danger("保存失败", e instanceof Error ? e.message : "未知错误", 5000);
    } finally {
      setSaving(false);
    }
  };

  const handleSaveQuick = async () => {
    setSavingQuick(true);
    setMsg("");
    try {
      const patch = QUICK_FIELDS.reduce<AnyObj>((acc, item) => {
        const val = parseQuickFieldValue(item, getQuickFieldText(quick, item));
        return setNestedValue(acc, item.path, val);
      }, {});
      undoQuickRef.current = JSON.parse(JSON.stringify(quick));
      undoContentRef.current = content;
      const res = await api.patchPrompts(patch);
      setQuick(res.parsed || {});
      const full = await api.getPrompts();
      setContent(String(full.content ?? full.yaml_text ?? ""));
      if (res.ok) {
        success("保存成功", "常用提示词已保存并重载", 4000);
      } else {
        danger("保存失败", res.message || "未知错误", 5000);
      }
    } catch (e: unknown) {
      danger("保存失败", e instanceof Error ? e.message : "未知错误", 5000);
    } finally {
      setSavingQuick(false);
    }
  };

  const setNavigatorValue = (path: string, value: unknown) => {
    setQuick((prev) => setNestedValue(prev, `prompt_navigator.${path}`, value));
  };

  const setNavigatorSectionValue = (sectionId: string, field: string, value: unknown) => {
    setQuick((prev) => setNestedValue(prev, `prompt_navigator.sections.${sectionId}.${field}`, value));
  };

  const handleSaveNavigator = async () => {
    setSavingNavigator(true);
    setMsg("");
    try {
      undoQuickRef.current = JSON.parse(JSON.stringify(quick));
      undoContentRef.current = content;
      const payload = asObject(getNestedRawValue(quick, "prompt_navigator"));
      const res = await api.patchPrompts({ prompt_navigator: payload });
      setQuick(res.parsed || {});
      const full = await api.getPrompts();
      setContent(String(full.content ?? full.yaml_text ?? ""));
      if (res.ok) {
        success("保存成功", res.message || "Prompt Navigator 已保存并重载", 5000);
      } else {
        danger("保存失败", res.message || "未知错误", 5000);
      }
    } catch (e: unknown) {
      danger("保存失败", e instanceof Error ? e.message : "未知错误", 5000);
    } finally {
      setSavingNavigator(false);
    }
  };

  const handleUndo = async () => {
    if (!undoContentRef.current) return;
    setSaving(true);
    try {
      const res = await api.updatePrompts(undoContentRef.current);
      if (res.ok) {
        setContent(undoContentRef.current);
        if (undoQuickRef.current) {
          setQuick(undoQuickRef.current);
          undoQuickRef.current = null;
        }
        undoContentRef.current = null;
        success("撤销成功", "已恢复到上次保存前的提示词", 4000);
      } else {
        danger("撤销失败", res.message || "未知错误", 5000);
      }
    } catch (e: unknown) {
      danger("撤销失败", e instanceof Error ? e.message : "未知错误", 5000);
    } finally {
      setSaving(false);
    }
  };

  const handleReload = async () => {
    try {
      const res = await api.reload();
      setMsg(res.ok ? "全量重载成功" : `重载失败: ${res.message}`);
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : "重载出错");
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner size="lg" />
      </div>
    );
  }

  const navigatorConfig = asObject(getNestedRawValue(quick, "prompt_navigator"));
  const navigatorSections = asObject(navigatorConfig.sections);

  return (
    <>
      <NotificationContainer notifications={notifications} />
      <div className="space-y-4 h-full flex flex-col rounded-2xl border border-default-300/35 bg-content1/45 p-4 backdrop-blur-sm">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">提示词编辑</h2>
        <div className="flex gap-2">
          {undoContentRef.current && (
            <Button variant="flat" startContent={<Undo2 size={16} />} onPress={handleUndo} isLoading={saving}>撤销</Button>
          )}
          <Button
            variant="flat"
            startContent={<RefreshCw size={16} />}
            onPress={handleReload}
          >
            全量重载
          </Button>
          <Button
            color="primary"
            startContent={<Save size={16} />}
            isLoading={saving}
            onPress={handleSave}
          >
            保存
          </Button>
        </div>
      </div>
      {msg && <p className={msg.includes("成功") ? "text-success" : "text-danger"}>{msg}</p>}
      <p className="text-xs text-default-400">
        编辑 config/prompts.yml — 保存后自动重载提示词，无需重启 bot
      </p>
      <p className="text-xs text-default-400">
        说明：本地 cue/关键词路由已硬删除，提示词仅用于 AI 行为约束，不再作为本地分支匹配词表。
      </p>
      <Accordion selectionMode="multiple" defaultExpandedKeys={["prompt_navigator", "quick_prompts"]}>
        <AccordionItem key="prompt_navigator" title="Prompt Navigator">
          <div className="space-y-4 rounded-xl border border-default-300/30 bg-content2/30 p-3">
            <div className="grid gap-3 md:grid-cols-4">
              <Switch
                isSelected={navigatorConfig.enable !== false}
                onValueChange={(v) => setNavigatorValue("enable", v)}
                classNames={{ base: "self-end" }}
              >
                启用
              </Switch>
              <Input
                label="模式"
                labelPlacement="outside"
                value={String(navigatorConfig.mode ?? "local_prefilter_llm_review")}
                onValueChange={(v) => setNavigatorValue("mode", v)}
                classNames={{ inputWrapper: "bg-content1/70 border border-default-300/35" }}
              />
              <Input
                label="默认分区"
                labelPlacement="outside"
                value={String(navigatorConfig.default_section ?? "general_chat")}
                onValueChange={(v) => setNavigatorValue("default_section", v)}
                classNames={{ inputWrapper: "bg-content1/70 border border-default-300/35" }}
              />
              <Input
                type="number"
                label="最大跳转次数"
                labelPlacement="outside"
                min={0}
                max={12}
                value={String(navigatorConfig.max_switches ?? 3)}
                onValueChange={(v) => setNavigatorValue("max_switches", Number(v || 0))}
                classNames={{ inputWrapper: "bg-content1/70 border border-default-300/35" }}
              />
            </div>
            <Textarea
              label="总目录提示词"
              labelPlacement="outside"
              minRows={3}
              maxRows={8}
              value={String(navigatorConfig.root_prompt ?? "")}
              onValueChange={(v) => setNavigatorValue("root_prompt", v)}
              classNames={{
                label: "text-sm font-semibold mb-1.5",
                input: "text-sm",
                inputWrapper: "bg-content1/70 border border-default-300/35 data-[focus=true]:border-primary/60",
              }}
            />
            <div className="space-y-3">
              {Object.entries(navigatorSections).map(([sectionId, rawSection]) => {
                const section = asObject(rawSection);
                return (
                  <div key={sectionId} className="space-y-3 rounded-lg border border-default-300/30 bg-content1/45 p-3">
                    <div className="grid gap-3 md:grid-cols-[minmax(140px,0.8fr)_minmax(180px,1fr)_minmax(180px,1fr)]">
                      <Input
                        label="分区 ID"
                        labelPlacement="outside"
                        value={sectionId}
                        isReadOnly
                        classNames={{ inputWrapper: "bg-content2/60 border border-default-300/30" }}
                      />
                      <Input
                        label="分区名"
                        labelPlacement="outside"
                        value={String(section.name ?? "")}
                        onValueChange={(v) => setNavigatorSectionValue(sectionId, "name", v)}
                        classNames={{ inputWrapper: "bg-content1/70 border border-default-300/35" }}
                      />
                      <Textarea
                        label="Fallback 分区"
                        labelPlacement="outside"
                        minRows={1}
                        maxRows={4}
                        placeholder="每行一个"
                        value={formatStringList(section.fallback_sections)}
                        onValueChange={(v) => setNavigatorSectionValue(sectionId, "fallback_sections", parseStringList(v))}
                        classNames={{ inputWrapper: "bg-content1/70 border border-default-300/35" }}
                      />
                    </div>
                    <Textarea
                      label="使用条件"
                      labelPlacement="outside"
                      minRows={2}
                      maxRows={6}
                      value={String(section.when_to_use ?? "")}
                      onValueChange={(v) => setNavigatorSectionValue(sectionId, "when_to_use", v)}
                      classNames={{ inputWrapper: "bg-content1/70 border border-default-300/35" }}
                    />
                    <div className="grid gap-3 md:grid-cols-2">
                      <Textarea
                        label="工具列表"
                        labelPlacement="outside"
                        minRows={4}
                        maxRows={10}
                        placeholder="每行一个"
                        value={formatStringList(section.tools)}
                        onValueChange={(v) => setNavigatorSectionValue(sectionId, "tools", parseStringList(v))}
                        classNames={{ inputWrapper: "bg-content1/70 border border-default-300/35" }}
                      />
                      <Textarea
                        label="失败策略"
                        labelPlacement="outside"
                        minRows={4}
                        maxRows={10}
                        value={String(section.failure_policy ?? "")}
                        onValueChange={(v) => setNavigatorSectionValue(sectionId, "failure_policy", v)}
                        classNames={{ inputWrapper: "bg-content1/70 border border-default-300/35" }}
                      />
                    </div>
                    <Textarea
                      label="分区提示词"
                      labelPlacement="outside"
                      minRows={4}
                      maxRows={12}
                      value={String(section.instructions ?? "")}
                      onValueChange={(v) => setNavigatorSectionValue(sectionId, "instructions", v)}
                      classNames={{ inputWrapper: "bg-content1/70 border border-default-300/35" }}
                    />
                  </div>
                );
              })}
            </div>
            <div className="flex justify-end pt-2">
              <Button
                color="primary"
                startContent={<Save size={18} />}
                isLoading={savingNavigator}
                onPress={handleSaveNavigator}
              >
                保存 Navigator
              </Button>
            </div>
          </div>
        </AccordionItem>
        <AccordionItem key="quick_prompts" title="常用提示词可视化编辑">
          <div className="space-y-3 rounded-xl border border-default-300/30 bg-content2/30 p-3">
            {QUICK_FIELDS.map((item) => (
              <Textarea
                key={item.path}
                label={item.label}
                labelPlacement="outside"
                placeholder={item.kind === "list" ? "每行一个" : ""}
                minRows={item.kind === "text" ? 4 : 2}
                maxRows={15}
                value={getQuickFieldText(quick, item)}
                onValueChange={(v) => {
                  setQuick((prev) => setNestedValue(prev, item.path, parseQuickFieldValue(item, v)));
                }}
                classNames={{
                  base: "w-full",
                  label: "text-sm font-semibold mb-1.5",
                  input: "text-sm",
                  inputWrapper: "bg-content1/70 border border-default-300/35 data-[focus=true]:border-primary/60",
                }}
              />
            ))}
            <div className="flex justify-end pt-3">
              <Button
                color="primary"
                size="lg"
                startContent={<Save size={18} />}
                isLoading={savingQuick}
                onPress={handleSaveQuick}
              >
                保存常用项
              </Button>
            </div>
          </div>
        </AccordionItem>
      </Accordion>
      <div className="flex-1 min-h-0 border border-default-300/35 rounded-lg overflow-hidden bg-content2/35">
        <CodeMirror
          value={content}
          onChange={setContent}
          extensions={[yaml()]}
          theme={isDark ? "dark" : "light"}
          height="100%"
          style={{ height: "calc(100vh - 220px)" }}
        />
      </div>
    </div>
    </>
  );
}
