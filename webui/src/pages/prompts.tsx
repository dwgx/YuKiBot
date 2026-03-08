import { useEffect, useState, useCallback } from "react";
import { Accordion, AccordionItem, Button, Spinner, Textarea } from "@heroui/react";
import { Save, RefreshCw } from "lucide-react";
import CodeMirror from "@uiw/react-codemirror";
import { yaml } from "@codemirror/lang-yaml";
import { api } from "../api/client";

type AnyObj = Record<string, unknown>;

type QuickField = {
  path: string;
  label: string;
  kind?: "text" | "list";
};

const QUICK_FIELDS: QuickField[] = [
  { path: "agent.identity", label: "Agent 身份定义", kind: "text" },
  { path: "agent.rules", label: "Agent 核心规则", kind: "text" },
  { path: "agent.tool_priority", label: "工具选择优先级", kind: "text" },
  { path: "agent.music_selection_guide", label: "音乐选择智能指南", kind: "text" },
  { path: "agent.context_rules", label: "上下文理解规则", kind: "text" },
  { path: "messages.mention_only_fallback", label: "空@回复（无名字）", kind: "text" },
  { path: "messages.mention_only_fallback_with_name", label: "空@回复（带名字）", kind: "text" },
  { path: "messages.llm_error_fallback", label: "LLM 错误回复", kind: "text" },
  { path: "messages.generic_error", label: "通用错误回复", kind: "text" },
  { path: "messages.search_followup_recent_media_title", label: "最近媒体结果标题", kind: "text" },
  { path: "messages.search_followup_recent_result_title", label: "最近搜索结果标题", kind: "text" },
  { path: "messages.explicit_fact_recall_reply", label: "事实回忆回复模板", kind: "text" },
  { path: "agent_runtime.reply_anchor_header", label: "回复锚点标题", kind: "text" },
  { path: "agent_runtime.reply_context_to_bot", label: "回复上下文（Bot）", kind: "text" },
  { path: "agent_runtime.reply_context_to_user", label: "回复上下文（用户）", kind: "text" },
  { path: "agent_runtime.attached_media_line", label: "附加媒体行", kind: "text" },
  { path: "agent_runtime.reply_media_line", label: "回复媒体行", kind: "text" },
  { path: "thinking_rules", label: "思考规则", kind: "text" },
  { path: "vision.main_system", label: "图片识别系统提示", kind: "text" },
  { path: "video.batch_system", label: "视频批处理系统提示", kind: "text" },
  { path: "personality_output_rules", label: "人格输出规则", kind: "text" },
  { path: "image_question_cues", label: "图片问题触发词", kind: "list" },
  { path: "image_reference_cues", label: "图片引用触发词", kind: "list" },
  { path: "image_request_cues", label: "图片请求触发词", kind: "list" },
  { path: "image_send_cues", label: "图片发送触发词", kind: "list" },
  { path: "local_media_request_cues", label: "本地媒体请求触发词", kind: "list" },
  { path: "media_instruction_cues", label: "媒体指令触发词", kind: "list" },
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

export default function PromptsPage() {
  const [content, setContent] = useState("");
  const [quick, setQuick] = useState<AnyObj>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingQuick, setSavingQuick] = useState(false);
  const [msg, setMsg] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await api.getPrompts();
      setContent(res.content);
      setQuick(res.parsed || {});
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleSave = async () => {
    setSaving(true);
    setMsg("");
    try {
      const res = await api.updatePrompts(content);
      setMsg(res.ok ? "保存成功，提示词已重载" : `失败: ${res.message}`);
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : "保存失败");
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
      const res = await api.patchPrompts(patch);
      setQuick(res.parsed || {});
      const full = await api.getPrompts();
      setContent(full.content);
      setMsg(res.ok ? "常用提示词已保存并重载" : `失败: ${res.message}`);
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSavingQuick(false);
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

  return (
    <div className="space-y-4 h-full flex flex-col">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">提示词编辑</h2>
        <div className="flex gap-2">
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
      <Accordion selectionMode="multiple" defaultExpandedKeys={["quick_prompts"]}>
        <AccordionItem key="quick_prompts" title="常用提示词可视化编辑">
          <div className="space-y-3">
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
      <div className="flex-1 min-h-0 border border-divider rounded-lg overflow-hidden">
        <CodeMirror
          value={content}
          onChange={setContent}
          extensions={[yaml()]}
          theme="dark"
          height="100%"
          style={{ height: "calc(100vh - 220px)" }}
        />
      </div>
    </div>
  );
}
