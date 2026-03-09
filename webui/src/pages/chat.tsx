import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent as ReactClipboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { Button, Card, CardBody, CardHeader, Chip, Input, Spinner, Textarea } from "@heroui/react";
import { BrainCircuit, ChevronDown, ChevronUp, Copy, ImagePlus, MessageSquare, Quote, RefreshCw, SendHorizontal, SmilePlus, Sparkles, Square, Star, Trash2, UserRound, X } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import { api, ChatAgentStateItem, ChatConversationItem, ChatHistoryPermission, ChatMessageItem } from "../api/client";

function fmtTs(ts: number): string {
  if (!ts || Number.isNaN(ts)) return "-";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString();
}

function clip(text: string, max = 64): string {
  const raw = String(text || "").trim();
  if (!raw) return "";
  return raw.length > max ? `${raw.slice(0, max)}...` : raw;
}

function hasImageSegment(msg: ChatMessageItem | null): boolean {
  if (!msg) return false;
  if (Array.isArray(msg.segments) && msg.segments.some((seg) => String(seg?.type || "").toLowerCase() === "image")) {
    return true;
  }
  return String(msg.text || "").includes("[image]");
}

const CONTEXT_EASTER_EGGS = [
  "右键彩蛋: 喵~",
  "右键彩蛋: 好耶!",
  "右键彩蛋: 今天也要顺滑回复",
];

const DEFAULT_CHAT_PERMISSION: ChatHistoryPermission = {
  bot_role: "",
  can_recall: false,
  can_set_essence: false,
};

const INPUT_CLASSES = {
  label: "text-default-500 text-xs",
  input: "text-sm !bg-transparent",
  inputWrapper: "bg-content2/55 border border-default-400/35 shadow-none transition-all duration-200 data-[hover=true]:bg-content2/70 data-[focus=true]:bg-content2/80 data-[focus=true]:border-primary/55 data-[focus=true]:shadow-[0_0_0_1px_rgba(96,165,250,0.18)]",
  innerWrapper: "bg-transparent shadow-none",
  base: "w-full",
} as const;

function parseThinkingLine(rawLine: string): string {
  const line = String(rawLine || "").trim();
  if (!line) return "";
  if (line.includes("agent_done")) return "任务完成";
  if (line.includes("cancelled_by_webui_interrupt")) return "已收到中断请求";
  if (line.includes("agent_direct_reply")) return "准备直接回复";
  if (line.includes("agent_unparseable_json")) return "正在修正模型输出";
  if (line.includes("agent_force_tool_first")) return "切到工具优先路径";
  if (line.includes("agent_final_answer")) return "正在整理最终回复";
  if (line.includes("agent_llm_timeout")) return "模型响应超时";
  if (line.includes("agent_total_timeout")) return "整体处理超时";

  const callMatch = line.match(/agent_tool_call.*\|\s*tool=([a-zA-Z0-9_.-]+)/);
  if (callMatch) {
    const tool = String(callMatch[1] || "");
    if (tool === "think") {
      const thoughtMatch = line.match(/"thought"\s*:\s*"(.+?)"/);
      if (thoughtMatch) {
        const thought = thoughtMatch[1]
          .replace(/\\n/g, " ")
          .replace(/\\"/g, "\"")
          .replace(/\\\\/g, "\\");
        return `思考: ${clip(thought, 140)}`;
      }
      return "思考中...";
    }
    return `调用工具: ${tool}`;
  }

  const resultMatch = line.match(/agent_tool_result.*\|\s*tool=([a-zA-Z0-9_.-]+)/);
  if (resultMatch) {
    return `工具完成: ${String(resultMatch[1] || "")}`;
  }

  const fallbackMatch = line.match(/agent_tool_fallback_try.*from=([a-zA-Z0-9_.-]+).*to=([a-zA-Z0-9_.-]+)/);
  if (fallbackMatch) {
    return `工具兜底: ${fallbackMatch[1]} -> ${fallbackMatch[2]}`;
  }

  if (line.includes("agent_")) return "正在规划下一步...";

  return "";
}

function thinkingStatusLabel(active: boolean): string {
  return active ? "处理中" : "已完成";
}

function normalizeCopyPayload(msg: ChatMessageItem): string {
  const text = String(msg.text || "").trim();
  if (text && text !== "[空消息]") return text;
  const segments = Array.isArray(msg.segments) ? msg.segments : [];
  if (!segments.length) return "";
  return segments
    .map((seg) => {
      const segType = String(seg?.type || "").toLowerCase();
      if (segType === "image") return "[图片]";
      if (segType === "video") return "[视频]";
      if (segType === "record" || segType === "audio") return "[语音]";
      if (segType === "text") return String(seg?.data?.text || "");
      return `[${segType || "消息"}]`;
    })
    .join(" ")
    .trim();
}

async function readImageFilePayload(file: File): Promise<{ dataUrl: string; base64Body: string }> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("图片读取失败"));
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsDataURL(file);
  });
  const base64Body = dataUrl.replace(/^data:[^;]+;base64,/, "");
  if (!base64Body) {
    throw new Error("图片编码失败");
  }
  return { dataUrl, base64Body };
}

export default function ChatPage() {
  const [loadingConvs, setLoadingConvs] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingState, setLoadingState] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  const [conversations, setConversations] = useState<ChatConversationItem[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [messages, setMessages] = useState<ChatMessageItem[]>([]);
  const [agentStates, setAgentStates] = useState<ChatAgentStateItem[]>([]);

  const [text, setText] = useState("");
  const [imageUrl, setImageUrl] = useState("");
  const [imageBase64, setImageBase64] = useState("");
  const [imageFileName, setImageFileName] = useState("");
  const [imagePreviewUrl, setImagePreviewUrl] = useState("");
  const [replyToMessage, setReplyToMessage] = useState<ChatMessageItem | null>(null);
  const [permission, setPermission] = useState<ChatHistoryPermission>(DEFAULT_CHAT_PERMISSION);
  const [contextEgg, setContextEgg] = useState("");
  const [thinkingLines, setThinkingLines] = useState<string[]>([]);
  const [thinkingDraft, setThinkingDraft] = useState("");
  const [retargeting, setRetargeting] = useState(false);
  const [thinkingPanelOpen, setThinkingPanelOpen] = useState(true);
  const [thinkingIslandExpanded, setThinkingIslandExpanded] = useState(true);
  const [thinkingWarmConversationId, setThinkingWarmConversationId] = useState("");
  const [thinkingWarmUntil, setThinkingWarmUntil] = useState(0);
  const [clockMs, setClockMs] = useState(() => Date.now());
  const [contextMenu, setContextMenu] = useState<{
    open: boolean;
    x: number;
    y: number;
    message: ChatMessageItem | null;
  }>({ open: false, x: 0, y: 0, message: null });
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messagesScrollRef = useRef<HTMLDivElement | null>(null);
  const thinkingScrollRef = useRef<HTMLDivElement | null>(null);
  const traceRef = useRef("");
  const conversationRef = useRef("");
  const scrollMessagesToBottom = useCallback(() => {
    const el = messagesScrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, []);

  const selected = useMemo(
    () => conversations.find((item) => item.conversation_id === selectedId) ?? null,
    [conversations, selectedId],
  );
  const selectedState = useMemo(
    () => agentStates.find((item) => item.conversation_id === selectedId) ?? null,
    [agentStates, selectedId],
  );
  const isThinking = Boolean(selectedState && ((selectedState.running_count ?? 0) > 0 || (selectedState.pending_count ?? 0) > 0));
  const optimisticThinking = Boolean(
    selected
    && thinkingWarmConversationId === selected.conversation_id
    && thinkingWarmUntil > clockMs,
  );
  const thinkingActive = Boolean(isThinking || optimisticThinking);
  const thinkingIslandVisible = Boolean(selected && thinkingPanelOpen && (thinkingActive || thinkingLines.length > 0));
  const latestThinkingLine = useMemo(
    () => thinkingLines[thinkingLines.length - 1] || (thinkingActive ? "正在建立计划..." : "刚刚处理完成"),
    [thinkingActive, thinkingLines],
  );
  const thinkingPreviewLines = useMemo(() => thinkingLines.slice(-6), [thinkingLines]);
  const canRecallInMenu = Boolean(selected?.chat_type === "group" && permission.can_recall);
  const canSetEssenceInMenu = Boolean(selected?.chat_type === "group" && permission.can_set_essence);
  const contextMessageIsEssence = Boolean(contextMenu.message?.is_essence);
  const closeContextMenu = useCallback(() => {
    setContextMenu((prev) => (prev.open ? { open: false, x: 0, y: 0, message: null } : prev));
  }, []);
  const touchThinkingPresence = useCallback((conversationId: string, ttlMs = 12000) => {
    if (!conversationId) return;
    setClockMs(Date.now());
    setThinkingWarmConversationId(conversationId);
    setThinkingWarmUntil(Date.now() + ttlMs);
    setThinkingPanelOpen(true);
    setThinkingIslandExpanded(true);
  }, []);

  const loadConversations = useCallback(async () => {
    setLoadingConvs(true);
    try {
      const res = await api.getChatConversations({ limit: 200 });
      const items = Array.isArray(res.items) ? res.items : [];
      setConversations(items);
      if (items.length > 0 && !items.some((item) => item.conversation_id === selectedId)) {
        setSelectedId(items[0].conversation_id);
      }
      if (items.length === 0) {
        setSelectedId("");
      }
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "读取会话失败");
    } finally {
      setLoadingConvs(false);
    }
  }, [selectedId]);

  const loadHistory = useCallback(async () => {
    if (!selected) {
      setMessages([]);
      setPermission(DEFAULT_CHAT_PERMISSION);
      return;
    }
    setLoadingHistory(true);
    try {
      const res = await api.getChatHistory({
        chatType: selected.chat_type,
        peerId: selected.peer_id,
        limit: 60,
      });
      setMessages(Array.isArray(res.items) ? res.items : []);
      setPermission(res.permission ?? DEFAULT_CHAT_PERMISSION);
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "读取聊天记录失败");
    } finally {
      setLoadingHistory(false);
    }
  }, [selected]);

  const loadAgentState = useCallback(async () => {
    setLoadingState(true);
    try {
      const res = await api.getChatAgentState({ limit: 200 });
      setAgentStates(Array.isArray(res.items) ? res.items : []);
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "读取运行状态失败");
    } finally {
      setLoadingState(false);
    }
  }, []);

  useEffect(() => {
    loadConversations();
    loadAgentState();
  }, [loadConversations, loadAgentState]);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      loadConversations();
      if (selectedId) {
        loadHistory();
      }
      loadAgentState();
    }, 8000);
    return () => window.clearInterval(timer);
  }, [loadConversations, loadHistory, loadAgentState, selectedId]);

  useEffect(() => {
    setReplyToMessage(null);
    closeContextMenu();
  }, [selectedId, closeContextMenu]);

  useEffect(() => {
    scrollMessagesToBottom();
  }, [selectedId, scrollMessagesToBottom]);

  useEffect(() => {
    if (loadingHistory) return;
    scrollMessagesToBottom();
  }, [messages, loadingHistory, scrollMessagesToBottom]);

  useEffect(() => {
    if (!contextMenu.open) return undefined;
    const onClickOutside = (evt: MouseEvent) => {
      const target = evt.target as HTMLElement | null;
      if (target?.closest("[data-chat-context-menu='1']")) return;
      closeContextMenu();
    };
    const onEsc = (evt: KeyboardEvent) => {
      if (evt.key === "Escape") closeContextMenu();
    };
    window.addEventListener("click", onClickOutside, true);
    window.addEventListener("contextmenu", onClickOutside, true);
    window.addEventListener("scroll", closeContextMenu, true);
    window.addEventListener("resize", closeContextMenu);
    window.addEventListener("keydown", onEsc);
    return () => {
      window.removeEventListener("click", onClickOutside, true);
      window.removeEventListener("contextmenu", onClickOutside, true);
      window.removeEventListener("scroll", closeContextMenu, true);
      window.removeEventListener("resize", closeContextMenu);
      window.removeEventListener("keydown", onEsc);
    };
  }, [contextMenu.open, closeContextMenu]);

  useEffect(() => {
    traceRef.current = String(selectedState?.latest_trace_id || selectedState?.last_trace_id || "");
    conversationRef.current = String(selected?.conversation_id || "");
  }, [selectedState, selected]);

  useEffect(() => {
    setThinkingLines([]);
    setThinkingDraft("");
    setThinkingPanelOpen(true);
    setThinkingIslandExpanded(true);
    setThinkingWarmConversationId("");
    setThinkingWarmUntil(0);
  }, [selectedId]);

  useEffect(() => {
    if (!selected?.conversation_id) return;
    if (!thinkingActive) return;
    touchThinkingPresence(selected.conversation_id, 16000);
  }, [selected?.conversation_id, thinkingActive, touchThinkingPresence]);

  useEffect(() => {
    if (thinkingWarmUntil <= clockMs) return undefined;
    const timer = window.setTimeout(() => setClockMs(Date.now()), Math.max(300, thinkingWarmUntil - clockMs));
    return () => window.clearTimeout(timer);
  }, [clockMs, thinkingWarmUntil]);

  useEffect(() => {
    const ws = new WebSocket(api.logsStreamUrl());
    ws.onmessage = (evt) => {
      let payload: { line?: string } | null = null;
      try {
        payload = JSON.parse(String(evt.data || "{}"));
      } catch {
        payload = null;
      }
      const line = String(payload?.line || "");
      if (!line) return;

      const trace = traceRef.current;
      const conversationId = conversationRef.current;
      if (trace) {
        if (!line.includes(trace)) return;
      } else if (conversationId && !line.includes(conversationId)) {
        return;
      } else if (!conversationId) {
        return;
      }

      touchThinkingPresence(conversationId, 14000);
      const parsed = parseThinkingLine(line);
      if (!parsed) return;
      setThinkingLines((prev) => {
        if (prev[prev.length - 1] === parsed) return prev;
        return [...prev.slice(-80), parsed];
      });
    };
    return () => {
      try {
        ws.close();
      } catch {
        // ignore
      }
    };
  }, [touchThinkingPresence]);

  useEffect(() => {
    if (!thinkingScrollRef.current) return;
    thinkingScrollRef.current.scrollTop = thinkingScrollRef.current.scrollHeight;
  }, [thinkingLines]);

  const sendText = async () => {
    const content = text.trim();
    if (!selected || !content) return;
    setSending(true);
    try {
      await api.sendChatText({
        chatType: selected.chat_type,
        peerId: selected.peer_id,
        text: content,
        replyToMessageId: replyToMessage?.message_id || undefined,
      });
      setText("");
      setReplyToMessage(null);
      await loadHistory();
      await loadConversations();
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "发送文本失败");
    } finally {
      setSending(false);
    }
  };

  const sendImage = async () => {
    const url = imageUrl.trim();
    const b64 = imageBase64.trim();
    if (!selected || (!url && !b64)) return;
    setSending(true);
    try {
      await api.sendChatImage({
        chatType: selected.chat_type,
        peerId: selected.peer_id,
        imageUrl: url || undefined,
        imageBase64: b64 || undefined,
      });
      setImageUrl("");
      setImageBase64("");
      setImageFileName("");
      setImagePreviewUrl("");
      await loadHistory();
      await loadConversations();
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "发送图片失败");
    } finally {
      setSending(false);
    }
  };

  const pickImageFile = () => {
    fileInputRef.current?.click();
  };

  const clearPendingImage = () => {
    setImageUrl("");
    setImageBase64("");
    setImageFileName("");
    setImagePreviewUrl("");
    setError("");
  };

  const onImageFileChange = async (evt: ChangeEvent<HTMLInputElement>) => {
    const file = evt.target.files?.[0];
    if (!file) return;
    try {
      const { dataUrl, base64Body } = await readImageFilePayload(file);
      setImageBase64(base64Body);
      setImageFileName(file.name);
      setImagePreviewUrl(dataUrl);
      setImageUrl("");
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "图片读取失败");
    } finally {
      evt.target.value = "";
    }
  };

  const onTextPaste = async (evt: ReactClipboardEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    const items = Array.from(evt.clipboardData?.items || []);
    const imageItem = items.find((item) => item.kind === "file" && item.type.startsWith("image/"));
    if (!imageItem) return;

    const file = imageItem.getAsFile();
    if (!file) {
      setError("剪贴板图片读取失败");
      return;
    }
    evt.preventDefault();
    try {
      const { dataUrl, base64Body } = await readImageFilePayload(file);
      const mimeSuffix = file.type.split("/")[1] || "png";
      setImageBase64(base64Body);
      setImageFileName(file.name || `clipboard-image.${mimeSuffix}`);
      setImagePreviewUrl(dataUrl);
      setImageUrl("");
      setError("[OK] 已从剪贴板读取图片，点击“发图”即可发送");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "剪贴板图片读取失败");
    }
  };

  const pendingImageSrc = imagePreviewUrl || imageUrl.trim();
  const pendingImageLabel = imageFileName || clip(imageUrl.trim(), 48) || "待发送图片";

  const interruptConversation = async () => {
    if (!selected) return;
    setSending(true);
    try {
      await api.interruptChat(selected.conversation_id);
      setThinkingWarmUntil(0);
      await loadAgentState();
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "中断会话失败");
    } finally {
      setSending(false);
    }
  };

  const openMessageMenu = (evt: ReactMouseEvent, msg: ChatMessageItem) => {
    evt.preventDefault();
    evt.stopPropagation();
    const menuWidth = 220;
    const menuHeight = 280;
    const x = Math.max(8, Math.min(evt.clientX, window.innerWidth - menuWidth - 8));
    const y = Math.max(8, Math.min(evt.clientY, window.innerHeight - menuHeight - 8));
    const randomEgg = CONTEXT_EASTER_EGGS[Math.floor(Math.random() * CONTEXT_EASTER_EGGS.length)] || "";
    setContextEgg(randomEgg);
    setContextMenu({ open: true, x, y, message: msg });
  };

  const copyMessageText = async () => {
    const msg = contextMenu.message;
    if (!msg) return;
    const payload = normalizeCopyPayload(msg);
    closeContextMenu();
    try {
      await navigator.clipboard.writeText(payload);
      setError("[OK] 已复制消息内容");
    } catch {
      setError("复制失败：浏览器未授予剪贴板权限");
    }
  };

  const useQuoteMessage = () => {
    if (!contextMenu.message) return;
    setReplyToMessage(contextMenu.message);
    closeContextMenu();
    setError("");
  };

  const recallMessage = async () => {
    const msg = contextMenu.message;
    if (!selected || !msg?.message_id) return;
    closeContextMenu();
    setSending(true);
    try {
      await api.recallChatMessage({
        messageId: msg.message_id,
        chatType: selected.chat_type,
        peerId: selected.peer_id,
      });
      await loadHistory();
      await loadConversations();
      setError("[OK] 已撤回该消息");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "撤回失败");
    } finally {
      setSending(false);
    }
  };

  const setEssenceMessage = async () => {
    const msg = contextMenu.message;
    if (!selected || !msg?.message_id) return;
    closeContextMenu();
    setSending(true);
    try {
      await api.setChatMessageEssence({
        messageId: msg.message_id,
        chatType: selected.chat_type,
        peerId: selected.peer_id,
      });
      setMessages((prev) =>
        prev.map((item) => item.message_id === msg.message_id ? { ...item, is_essence: true } : item),
      );
      setError("[OK] 已设为精华");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "设置精华失败");
    } finally {
      setSending(false);
    }
  };

  const removeEssenceMessage = async () => {
    const msg = contextMenu.message;
    if (!selected || !msg?.message_id) return;
    closeContextMenu();
    setSending(true);
    try {
      await api.removeChatMessageEssence({
        messageId: msg.message_id,
        chatType: selected.chat_type,
        peerId: selected.peer_id,
      });
      setMessages((prev) =>
        prev.map((item) => item.message_id === msg.message_id ? { ...item, is_essence: false } : item),
      );
      setError("[OK] 已移除精华");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "移除精华失败");
    } finally {
      setSending(false);
    }
  };

  const addMessageToSticker = async () => {
    const msg = contextMenu.message;
    if (!msg?.message_id || !hasImageSegment(msg)) return;
    closeContextMenu();
    setSending(true);
    try {
      const res = await api.addChatMessageToSticker({
        messageId: msg.message_id,
        sourceUserId: msg.sender_id || "",
        description: msg.text && !msg.text.startsWith("[") ? clip(msg.text, 20) : "",
      });
      setError(`[OK] 已添加到表情包: ${res.key}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "添加表情包失败");
    } finally {
      setSending(false);
    }
  };

  const retargetConversation = async () => {
    const goal = thinkingDraft.trim();
    if (!selected || !goal) return;
    setRetargeting(true);
    try {
      touchThinkingPresence(selected.conversation_id, 18000);
      if (isThinking) {
        await api.interruptChat(selected.conversation_id);
      }
      await api.sendChatText({
        chatType: selected.chat_type,
        peerId: selected.peer_id,
        text: goal,
      });
      setThinkingLines((prev) => [...prev.slice(-80), `临时改目标: ${clip(goal, 150)}`]);
      setThinkingDraft("");
      await loadAgentState();
      await loadHistory();
      setError("[OK] 已发送临时目标");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "临时改目标失败");
    } finally {
      setRetargeting(false);
    }
  };

  return (
    <section className="h-[calc(100vh-96px)] min-h-0 flex flex-col gap-3 overflow-hidden">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <MessageSquare size={20} />
          <h2 className="text-xl font-bold">聊天控制台</h2>
          <Chip size="sm" variant="flat">{conversations.length} 会话</Chip>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="flat" startContent={<RefreshCw size={14} />} onPress={loadConversations} isLoading={loadingConvs}>
            刷新会话
          </Button>
          <Button size="sm" variant="flat" startContent={<RefreshCw size={14} />} onPress={loadHistory} isLoading={loadingHistory} isDisabled={!selected}>
            刷新消息
          </Button>
          <Button size="sm" variant="flat" startContent={<RefreshCw size={14} />} onPress={loadAgentState} isLoading={loadingState}>
            刷新状态
          </Button>
        </div>
      </div>

      {error && (
        <p className={`${error.startsWith("[OK]") ? "text-success" : "text-danger"} text-sm whitespace-pre-wrap`}>
          {error.replace(/^\[OK\]\s*/, "")}
        </p>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-[320px_minmax(0,1fr)] gap-3 flex-1 min-h-0">
        <Card className="h-full overflow-hidden">
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between w-full">
              <span className="text-sm font-semibold">会话列表</span>
              {loadingConvs && <Spinner size="sm" />}
            </div>
          </CardHeader>
          <CardBody className="pt-1 overflow-auto space-y-2">
            {conversations.map((item) => {
              const active = selectedId === item.conversation_id;
              return (
                <button
                  type="button"
                  key={item.conversation_id}
                  className={`w-full text-left rounded-lg border px-3 py-2 transition ${
                    active
                      ? "border-primary/70 bg-primary/10"
                      : "border-default-300/40 bg-content2/35 hover:bg-content2/55"
                  }`}
                  onClick={() => setSelectedId(item.conversation_id)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="truncate font-medium">
                      {item.chat_type === "group" ? "群聊" : "私聊"} · {item.peer_name}
                    </div>
                    {item.unread_count > 0 && (
                      <Chip size="sm" color="danger" variant="flat">{item.unread_count}</Chip>
                    )}
                  </div>
                  <div className="mt-1 text-xs text-default-500 truncate">{clip(item.last_message, 80) || "暂无预览"}</div>
                  <div className="mt-1 text-[11px] text-default-400">{fmtTs(item.last_time)}</div>
                </button>
              );
            })}
            {conversations.length === 0 && <p className="text-default-400 text-sm">暂无最近会话</p>}
          </CardBody>
        </Card>

        <div className="grid grid-rows-[auto_minmax(0,1fr)_auto] gap-3 h-full min-h-0">
          <Card>
            <CardBody className="py-3">
              <div className="flex flex-wrap items-center gap-2 justify-between">
                <div className="min-w-0">
                  <div className="font-semibold truncate">
                    {selected ? `${selected.chat_type === "group" ? "群聊" : "私聊"} · ${selected.peer_name}` : "未选择会话"}
                  </div>
                  {selected && (
                    <div className="text-xs text-default-500">
                      ID: {selected.peer_id} · 最近消息: {fmtTs(selected.last_time)}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Chip size="sm" variant="flat" color={selectedState && selectedState.pending_count > 0 ? "warning" : "success"}>
                    队列: {selectedState?.pending_count ?? 0}
                  </Chip>
                  {!thinkingPanelOpen && selected && (
                    <Button
                      size="sm"
                      variant="flat"
                      startContent={<Sparkles size={14} />}
                      onPress={() => setThinkingPanelOpen(true)}
                    >
                      显示灵动岛
                    </Button>
                  )}
                  <Button
                    size="sm"
                    color="warning"
                    variant="flat"
                    startContent={<Square size={14} />}
                    onPress={interruptConversation}
                    isDisabled={!selected || sending}
                  >
                    中断当前会话
                  </Button>
                </div>
              </div>
              {selectedState && (
                <div className="mt-2 text-xs text-default-500">
                  running={selectedState.running_count} · queued={selectedState.queued_count} · latest={selectedState.last_trace_id || selectedState.latest_trace_id || "-"}
                </div>
              )}
              {selected?.chat_type === "group" && (
                <div className="mt-1 text-xs text-default-500">
                  Bot群权限: {permission.bot_role || "member"}
                </div>
              )}
            </CardBody>
          </Card>

          <Card className="h-full min-h-0 overflow-hidden relative">
            <CardBody ref={messagesScrollRef} className="min-h-0 overflow-auto space-y-2">
              {loadingHistory && <Spinner size="sm" />}
              {!loadingHistory && messages.length === 0 && (
                <p className="text-default-400 text-sm">暂无聊天记录</p>
              )}
              {messages.map((msg) => (
                <div key={`${msg.message_id}-${msg.seq}-${msg.timestamp}`} className={`flex ${msg.is_self ? "justify-end" : "justify-start"}`}>
                  <div
                    className={`max-w-[82%] rounded-lg px-3 py-2 ${
                      msg.is_recalled
                        ? "border border-warning/35 bg-warning/10 text-default-700"
                        : msg.is_self
                          ? "bg-primary/15 border border-primary/30"
                          : "bg-content2/45 border border-default-300/30"
                    }`}
                    onContextMenu={(evt) => openMessageMenu(evt, msg)}
                    title="右键可操作：复制 / 引用 / 设精华 / 移除精华 / 添加表情包 / 撤回"
                  >
                    <div className="mb-1 flex flex-wrap items-center gap-1.5 text-xs text-default-500">
                      <span className="inline-flex items-center gap-1">
                        <UserRound size={12} />
                        <span>{msg.sender_name || msg.sender_id || "未知"}</span>
                      </span>
                      <span>·</span>
                      <span>{fmtTs(msg.timestamp)}</span>
                      {msg.is_essence && <Chip size="sm" variant="flat" color="warning">精华</Chip>}
                      {msg.is_recalled && <Chip size="sm" variant="flat" color="warning">此消息已撤回</Chip>}
                    </div>
                    <div className="text-sm whitespace-pre-wrap break-words">
                      {msg.text || "[空消息]"}
                    </div>
                    {msg.is_recalled && (
                      <div className="mt-2 text-[11px] text-warning-700/90">
                        此消息已撤回，保留原文仅用于对话追踪。
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </CardBody>
            <AnimatePresence>
              {thinkingIslandVisible && (
                <motion.div
                  initial={{ opacity: 0, y: -16, scale: 0.96 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: -12, scale: 0.98 }}
                  transition={{ type: "spring", stiffness: 320, damping: 28 }}
                  className="pointer-events-none absolute inset-x-0 top-3 z-20 flex justify-center px-3"
                >
                  <div className="pointer-events-auto w-[min(640px,100%)] overflow-hidden rounded-[28px] border border-white/10 bg-[radial-gradient(circle_at_top,rgba(92,145,255,0.32),rgba(15,23,42,0.96)_48%,rgba(2,6,23,0.98)_100%)] shadow-[0_24px_80px_rgba(15,23,42,0.55)] backdrop-blur-2xl">
                    <div className="flex items-start gap-3 px-4 py-3">
                      <div className="mt-0.5 flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-white/8 ring-1 ring-white/10">
                        <BrainCircuit size={18} className={`${thinkingActive ? "text-sky-200" : "text-emerald-200"}`} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold text-white">YuKiKo Thinking</span>
                          <Chip size="sm" variant="flat" color={thinkingActive ? "primary" : "success"}>
                            {thinkingStatusLabel(thinkingActive)}
                          </Chip>
                          <Chip size="sm" variant="flat">
                            {thinkingLines.length} 条
                          </Chip>
                        </div>
                        <div className="mt-1 flex items-center gap-1.5 text-[11px] text-sky-100/80">
                          <span className="h-1.5 w-1.5 rounded-full bg-sky-300 animate-pulse" />
                          <span className="truncate">{latestThinkingLine}</span>
                        </div>
                      </div>
                      <div className="flex items-center gap-1">
                        <Button
                          size="sm"
                          variant="light"
                          isIconOnly
                          onPress={() => setThinkingIslandExpanded((prev) => !prev)}
                          className="text-white/85"
                        >
                          {thinkingIslandExpanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                        </Button>
                        <Button
                          size="sm"
                          variant="light"
                          isIconOnly
                          onPress={() => setThinkingPanelOpen(false)}
                          className="text-white/85"
                        >
                          <X size={14} />
                        </Button>
                      </div>
                    </div>
                    <AnimatePresence initial={false}>
                      {thinkingIslandExpanded && (
                        <motion.div
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: "auto", opacity: 1 }}
                          exit={{ height: 0, opacity: 0 }}
                          transition={{ duration: 0.18 }}
                          className="overflow-hidden border-t border-white/10"
                        >
                          <div ref={thinkingScrollRef} className="max-h-48 space-y-2 overflow-auto px-4 py-3">
                            {thinkingPreviewLines.length === 0 && (
                              <p className="text-xs text-sky-100/70">已接入会话，等待更具体的思考流。</p>
                            )}
                            {thinkingPreviewLines.map((line, idx) => (
                              <motion.div
                                key={`${idx}-${line}`}
                                initial={{ opacity: 0, y: 4 }}
                                animate={{ opacity: 1, y: 0 }}
                                transition={{ duration: 0.14 }}
                                className="rounded-2xl border border-white/8 bg-white/6 px-3 py-2 text-xs text-slate-100"
                              >
                                {line}
                              </motion.div>
                            ))}
                          </div>
                          <div className="space-y-2 px-4 pb-4">
                            <Textarea
                              label="临时改目标"
                              labelPlacement="outside"
                              minRows={2}
                              value={thinkingDraft}
                              onValueChange={setThinkingDraft}
                              placeholder="比如：先别继续搜图，先给我3个候选并说明理由"
                              classNames={INPUT_CLASSES}
                            />
                            <div className="flex flex-wrap items-center justify-end gap-2">
                              <Button
                                size="sm"
                                color="warning"
                                variant="flat"
                                startContent={<Square size={13} />}
                                onPress={interruptConversation}
                                isDisabled={!thinkingActive || sending}
                              >
                                取消当前任务
                              </Button>
                              <Button
                                size="sm"
                                color="primary"
                                onPress={retargetConversation}
                                isDisabled={!thinkingDraft.trim() || retargeting}
                                isLoading={retargeting}
                              >
                                发送临时目标
                              </Button>
                            </div>
                          </div>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </Card>

          <Card>
            <CardBody className="space-y-2">
              {replyToMessage && (
                <div className="rounded-lg border border-primary/35 bg-primary/10 px-3 py-2 flex items-start justify-between gap-3">
                  <div className="text-xs min-w-0">
                    <p className="text-primary font-medium">正在引用消息</p>
                    <p className="text-default-600 truncate">
                      {replyToMessage.sender_name || replyToMessage.sender_id || "未知"}: {clip(replyToMessage.text || "[媒体消息]", 88)}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="light"
                    isIconOnly
                    onPress={() => setReplyToMessage(null)}
                  >
                    <X size={14} />
                  </Button>
                </div>
              )}
              <Textarea
                label="发送文本"
                labelPlacement="outside"
                minRows={2}
                value={text}
                onValueChange={setText}
                onPaste={onTextPaste}
                placeholder={selected ? "输入要发送到当前会话的文字..." : "请先选择会话"}
                isDisabled={!selected || sending}
                classNames={INPUT_CLASSES}
              />
              <div className="flex items-center gap-2">
                <Button
                  color="primary"
                  startContent={<SendHorizontal size={14} />}
                  onPress={sendText}
                  isDisabled={!selected || !text.trim() || sending}
                  isLoading={sending}
                >
                  发送文本
                </Button>
                <Input
                  className="flex-1"
                  placeholder="图片 URL（http/https）"
                  value={imageUrl}
                  onValueChange={(v) => {
                    setImageUrl(v);
                    if (v.trim()) {
                      setImageBase64("");
                      setImageFileName("");
                      setImagePreviewUrl("");
                    }
                  }}
                  isDisabled={!selected || sending}
                  classNames={INPUT_CLASSES}
                />
                <Button
                  variant="flat"
                  startContent={<ImagePlus size={14} />}
                  onPress={pickImageFile}
                  isDisabled={!selected || sending}
                >
                  上传图
                </Button>
                <Button
                  variant="flat"
                  startContent={<ImagePlus size={14} />}
                  onPress={sendImage}
                  isDisabled={!selected || (!imageUrl.trim() && !imageBase64.trim()) || sending}
                  isLoading={sending}
                >
                  发图
                </Button>
              </div>
              {pendingImageSrc && (
                <div className="inline-flex max-w-full items-center gap-2 self-start rounded-full border border-default-300/40 bg-content2/55 px-2.5 py-1.5">
                  <div className="flex min-w-0 items-center gap-2">
                    <img
                      src={pendingImageSrc}
                      alt={pendingImageLabel}
                      className="h-7 w-7 rounded-full border border-default-300/40 object-cover"
                    />
                    <p className="max-w-[220px] truncate text-sm font-medium">{pendingImageLabel}</p>
                  </div>
                  <Button
                    size="sm"
                    variant="light"
                    isIconOnly
                    onPress={clearPendingImage}
                    isDisabled={sending}
                    className="h-6 min-w-6 w-6"
                  >
                    <X size={12} />
                  </Button>
                </div>
              )}
              <input ref={fileInputRef} type="file" accept="image/*" className="hidden" onChange={onImageFileChange} />
            </CardBody>
          </Card>
        </div>
      </div>

      {contextMenu.open && contextMenu.message && (
        <div
          data-chat-context-menu="1"
          className="fixed z-[70] w-[220px] rounded-xl border border-default-400/45 bg-content1/95 backdrop-blur p-1 shadow-xl"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          {contextEgg && (
            <div className="px-3 py-1 text-[11px] text-primary/80 flex items-center gap-1">
              <Sparkles size={12} />
              {contextEgg}
            </div>
          )}
          <button
            type="button"
            className="w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2"
            onClick={copyMessageText}
          >
            <Copy size={14} />
            复制消息
          </button>
          <button
            type="button"
            className="w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2"
            onClick={useQuoteMessage}
            disabled={!contextMenu.message?.message_id}
          >
            <Quote size={14} />
            引用这条消息
          </button>
          <button
            type="button"
            className="w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2 disabled:opacity-45"
            onClick={addMessageToSticker}
            disabled={sending || !hasImageSegment(contextMenu.message)}
          >
            <SmilePlus size={14} />
            添加到表情包
          </button>
          {canSetEssenceInMenu && (
            <>
              {!contextMessageIsEssence && (
                <button
                  type="button"
                  className="w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2 disabled:opacity-45"
                  onClick={setEssenceMessage}
                  disabled={sending || !contextMenu.message?.message_id}
                >
                  <Star size={14} />
                  设为精华
                </button>
              )}
              {contextMessageIsEssence && (
                <button
                  type="button"
                  className="w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2 disabled:opacity-45"
                  onClick={removeEssenceMessage}
                  disabled={sending || !contextMenu.message?.message_id}
                >
                  <Star size={14} />
                  移除精华
                </button>
              )}
            </>
          )}
          {canRecallInMenu && (
            <>
              <div className="my-1 h-px bg-default-300/45" />
              <button
                type="button"
                className="w-full text-left px-3 py-2 rounded-lg hover:bg-danger/15 text-sm flex items-center gap-2 text-danger disabled:opacity-45"
                onClick={recallMessage}
                disabled={sending || !contextMenu.message?.message_id || Boolean(contextMenu.message?.is_recalled)}
              >
                <Trash2 size={14} />
                {contextMenu.message?.is_recalled ? "已撤回" : "撤回"}
              </button>
            </>
          )}
        </div>
      )}
    </section>
  );
}
