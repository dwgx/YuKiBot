const BASE = "/api/webui";

class ApiClient {
  private token = "";

  setToken(t: string) {
    this.token = t;
    localStorage.setItem("webui_token", t);
  }

  getToken(): string {
    if (!this.token) {
      this.token = localStorage.getItem("webui_token") || "";
    }
    return this.token;
  }

  clearToken() {
    this.token = "";
    localStorage.removeItem("webui_token");
  }

  private async request<T = unknown>(path: string, opts?: RequestInit): Promise<T> {
    const res = await fetch(`${BASE}${path}`, {
      ...opts,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.getToken()}`,
        ...opts?.headers,
      },
    });
    if (res.status === 401) {
      this.clearToken();
      window.location.href = "/webui/login";
      throw new Error("Unauthorized");
    }
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `HTTP ${res.status}`);
    }
    return res.json();
  }

  auth(token: string) {
    return this.request<{ ok: boolean }>("/auth", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
  }

  getStatus() {
    return this.request<StatusData>("/status");
  }

  getConfig() {
    return this.request<{ config: Record<string, unknown> }>("/config");
  }

  updateConfig(config: Record<string, unknown>) {
    return this.request<{ ok: boolean; message: string }>("/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    });
  }

  testImageGen(payload: ImageGenTestRequest) {
    return this.request<ImageGenTestResponse>("/image-gen/test", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  getPrompts() {
    return this.request<{ content?: string; yaml_text?: string; parsed: Record<string, unknown> }>("/prompts");
  }

  updatePrompts(content: string) {
    return this.request<{ ok: boolean; message: string }>("/prompts", {
      method: "PUT",
      body: JSON.stringify({ yaml_text: content, content }),
    });
  }

  patchPrompts(patch: Record<string, unknown>) {
    return this.request<{ ok: boolean; message: string; parsed: Record<string, unknown> }>("/prompts", {
      method: "PATCH",
      body: JSON.stringify({ patch }),
    });
  }

  reload() {
    return this.request<{ ok: boolean; message: string }>("/reload", { method: "POST" });
  }

  getLogs(lines = 200) {
    return this.request<{ lines: string[] }>(`/logs?lines=${lines}`);
  }

  getDbOverview() {
    return this.request<DbOverviewResponse>("/db/overview");
  }

  getDbTables(db: string, includeSystem = false, withCounts = false) {
    const q = new URLSearchParams({
      include_system: String(includeSystem),
      with_counts: String(withCounts),
    });
    return this.request<DbTablesResponse>(`/db/${encodeURIComponent(db)}/tables?${q.toString()}`);
  }

  getDbRows(
    db: string,
    params: {
      table: string;
      page?: number;
      pageSize?: number;
      query?: string;
      includeSystem?: boolean;
    },
  ) {
    const q = new URLSearchParams({
      table: params.table,
      page: String(params.page ?? 1),
      page_size: String(params.pageSize ?? 50),
      q: params.query ?? "",
      include_system: String(Boolean(params.includeSystem)),
    });
    return this.request<DbRowsResponse>(`/db/${encodeURIComponent(db)}/rows?${q.toString()}`);
  }

  clearDbTable(db: string, table: string) {
    return this.request<{ ok: boolean; message: string; db: string; table: string; deleted: number }>(
      `/db/${encodeURIComponent(db)}/clear`,
      {
        method: "POST",
        body: JSON.stringify({ table }),
      },
    );
  }

  getMemoryRecords(params: {
    conversationId?: string;
    userId?: string;
    role?: string;
    keyword?: string;
    page?: number;
    pageSize?: number;
  }) {
    const q = new URLSearchParams({
      conversation_id: params.conversationId ?? "",
      user_id: params.userId ?? "",
      role: params.role ?? "",
      keyword: params.keyword ?? "",
      page: String(params.page ?? 1),
      page_size: String(params.pageSize ?? 50),
    });
    return this.request<MemoryRecordsResponse>(`/memory/records?${q.toString()}`);
  }

  addMemoryRecord(payload: MemoryRecordCreateRequest) {
    return this.request<{ ok: boolean; message: string; item: MemoryRecordItem }>("/memory/records", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  updateMemoryRecord(recordId: number, payload: MemoryRecordUpdateRequest) {
    return this.request<{ ok: boolean; message: string; item: MemoryRecordItem }>(
      `/memory/records/${recordId}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    );
  }

  deleteMemoryRecord(recordId: number, payload: MemoryRecordDeleteRequest) {
    return this.request<{ ok: boolean; message: string; item: MemoryRecordItem }>(
      `/memory/records/${recordId}`,
      {
        method: "DELETE",
        body: JSON.stringify(payload),
      },
    );
  }

  getMemoryAudit(params: { recordId?: number; page?: number; pageSize?: number }) {
    const q = new URLSearchParams({
      record_id: String(params.recordId ?? 0),
      page: String(params.page ?? 1),
      page_size: String(params.pageSize ?? 50),
    });
    return this.request<MemoryAuditResponse>(`/memory/audit?${q.toString()}`);
  }

  compactMemory(payload: MemoryCompactRequest) {
    return this.request<{ ok: boolean; message: string; result: MemoryCompactResult }>("/memory/compact", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  getChatConversations(params?: { limit?: number; botId?: string }) {
    const q = new URLSearchParams({
      limit: String(params?.limit ?? 100),
      bot_id: params?.botId ?? "",
    });
    return this.request<ChatConversationResponse>(`/chat/conversations?${q.toString()}`);
  }

  getChatHistory(params: { chatType: "group" | "private"; peerId: string; limit?: number; messageSeq?: string; botId?: string }) {
    const q = new URLSearchParams({
      chat_type: params.chatType,
      peer_id: params.peerId,
      limit: String(params.limit ?? 30),
      message_seq: params.messageSeq ?? "",
      bot_id: params.botId ?? "",
    });
    return this.request<ChatHistoryResponse>(`/chat/history?${q.toString()}`);
  }

  sendChatText(payload: { chatType: "group" | "private"; peerId: string; text: string; botId?: string; replyToMessageId?: string }) {
    return this.request<{ ok: boolean; message_id?: string }>("/chat/send-text", {
      method: "POST",
      body: JSON.stringify({
        chat_type: payload.chatType,
        peer_id: payload.peerId,
        text: payload.text,
        bot_id: payload.botId ?? "",
        reply_to_message_id: payload.replyToMessageId ?? "",
      }),
    });
  }

  sendChatAgentText(payload: {
    chatType: "group" | "private";
    peerId: string;
    text: string;
    botId?: string;
    replyToMessageId?: string;
    contextUserId?: string;
    contextUserName?: string;
    contextSenderRole?: string;
  }) {
    return this.request<{ ok: boolean; status?: string; reason?: string; conversation_id?: string; trace_id?: string; seq?: number }>("/chat/agent-text", {
      method: "POST",
      body: JSON.stringify({
        chat_type: payload.chatType,
        peer_id: payload.peerId,
        text: payload.text,
        bot_id: payload.botId ?? "",
        reply_to_message_id: payload.replyToMessageId ?? "",
        context_user_id: payload.contextUserId ?? "",
        context_user_name: payload.contextUserName ?? "",
        context_sender_role: payload.contextSenderRole ?? "",
      }),
    });
  }

  sendChatImage(payload: { chatType: "group" | "private"; peerId: string; imageUrl?: string; imageBase64?: string; botId?: string }) {
    return this.request<{ ok: boolean; message_id?: string }>("/chat/send-image", {
      method: "POST",
      body: JSON.stringify({
        chat_type: payload.chatType,
        peer_id: payload.peerId,
        image_url: payload.imageUrl ?? "",
        image_base64: payload.imageBase64 ?? "",
        bot_id: payload.botId ?? "",
      }),
    });
  }

  interruptChat(conversationId: string) {
    return this.request<{ ok: boolean; message: string; result: ChatInterruptResult }>("/chat/interrupt", {
      method: "POST",
      body: JSON.stringify({ conversation_id: conversationId }),
    });
  }

  recallChatMessage(payload: { messageId: string; chatType?: "group" | "private"; peerId?: string; botId?: string }) {
    return this.request<{ ok: boolean; message: string; message_id: string }>("/chat/message/recall", {
      method: "POST",
      body: JSON.stringify({
        message_id: payload.messageId,
        chat_type: payload.chatType ?? "",
        peer_id: payload.peerId ?? "",
        bot_id: payload.botId ?? "",
      }),
    });
  }

  setChatMessageEssence(payload: { messageId: string; chatType?: "group" | "private"; peerId?: string; botId?: string }) {
    return this.request<{ ok: boolean; message: string; message_id: string }>("/chat/message/essence", {
      method: "POST",
      body: JSON.stringify({
        message_id: payload.messageId,
        chat_type: payload.chatType ?? "",
        peer_id: payload.peerId ?? "",
        bot_id: payload.botId ?? "",
      }),
    });
  }

  removeChatMessageEssence(payload: { messageId: string; chatType?: "group" | "private"; peerId?: string; botId?: string }) {
    return this.request<{ ok: boolean; message: string; message_id: string }>("/chat/message/essence/remove", {
      method: "POST",
      body: JSON.stringify({
        message_id: payload.messageId,
        chat_type: payload.chatType ?? "",
        peer_id: payload.peerId ?? "",
        bot_id: payload.botId ?? "",
      }),
    });
  }

  addChatMessageToSticker(payload: { messageId: string; sourceUserId?: string; description?: string; botId?: string }) {
    return this.request<{ ok: boolean; message: string; key: string; owner: string; source: string; description: string }>("/chat/message/add-sticker", {
      method: "POST",
      body: JSON.stringify({
        message_id: payload.messageId,
        source_user_id: payload.sourceUserId ?? "",
        description: payload.description ?? "",
        bot_id: payload.botId ?? "",
      }),
    });
  }

  getChatAgentState(params?: { conversationId?: string; limit?: number }) {
    const q = new URLSearchParams({
      conversation_id: params?.conversationId ?? "",
      limit: String(params?.limit ?? 200),
    });
    return this.request<ChatAgentStateResponse>(`/chat/agent-state?${q.toString()}`);
  }

  logsStreamUrl(): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const token = encodeURIComponent(this.getToken());
    return `${proto}//${location.host}/api/webui/logs/stream?token=${token}`;
  }
}

export interface StatusData {
  uptime_seconds: number;
  message_count: number;
  whitelist_groups: number[];
  model: string;
  agent_enabled: boolean;
  tool_count: number;
  safety_scale: number;
  bot_name: string;
  plugins: { name: string; description: string }[];
}

export interface DbOverviewItem {
  name: string;
  path: string;
  exists: boolean;
  size_bytes: number;
  modified_at: number;
  table_count: number;
  tables: string[];
}

export interface DbOverviewResponse {
  databases: DbOverviewItem[];
}

export interface DbColumnInfo {
  cid: number;
  name: string;
  type: string;
  notnull: number;
  default: unknown;
  pk: number;
}

export interface DbTableInfo {
  name: string;
  column_count: number;
  columns: DbColumnInfo[];
  row_count?: number;
}

export interface DbTablesResponse {
  db: string;
  path: string;
  tables: DbTableInfo[];
}

export interface DbRowsResponse {
  db: string;
  table: string;
  page: number;
  page_size: number;
  total: number;
  columns: DbColumnInfo[];
  rows: Record<string, unknown>[];
}

export interface ImageGenTestRequest {
  prompt: string;
  model?: string;
  size?: string;
  style?: string;
  image_gen?: Record<string, unknown>;
}

export interface ImageGenTestResponse {
  ok: boolean;
  message: string;
  image_url?: string;
  model_used?: string;
  revised_prompt?: string;
  requested_model?: string;
  default_model?: string;
  configured_models?: number;
  default_adjusted?: boolean;
}

export interface MemoryRecordItem {
  id: number;
  conversation_id: string;
  conversation_label?: string;
  user_id: string;
  display_name?: string;
  role: string;
  content: string;
  created_at: string;
}

export interface MemoryRecordsResponse {
  items: MemoryRecordItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface MemoryAuditItem {
  id: number;
  record_id: number | null;
  action: string;
  actor: string;
  note: string;
  reason: string;
  before_content: string;
  after_content: string;
  conversation_id: string;
  user_id: string;
  role: string;
  created_at: string;
}

export interface MemoryAuditResponse {
  items: MemoryAuditItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface MemoryRecordCreateRequest {
  conversation_id: string;
  user_id: string;
  role?: string;
  content: string;
  note?: string;
  reason?: string;
  actor?: string;
}

export interface MemoryRecordUpdateRequest {
  content: string;
  note: string;
  reason?: string;
  actor?: string;
}

export interface MemoryRecordDeleteRequest {
  note: string;
  reason?: string;
  actor?: string;
}

export interface MemoryCompactRequest {
  conversation_id?: string;
  user_id?: string;
  role?: string;
  dry_run?: boolean;
  keep_latest?: number;
  note?: string;
  reason?: string;
  actor?: string;
}

export interface MemoryCompactResult {
  dry_run: boolean;
  scanned: number;
  duplicates: number;
  keep_latest: number;
  filters: {
    conversation_id: string;
    user_id: string;
    role: string;
  };
  deleted_ids: number[];
}

export interface ChatConversationItem {
  conversation_id: string;
  chat_type: "group" | "private";
  peer_id: string;
  peer_name: string;
  last_time: number;
  unread_count: number;
  last_message: string;
}

export interface ChatConversationResponse {
  items: ChatConversationItem[];
  total: number;
}

export interface ChatMessageItem {
  message_id: string;
  seq: string;
  timestamp: number;
  time_iso: string;
  sender_id: string;
  sender_name: string;
  sender_role: string;
  is_self: boolean;
  is_essence?: boolean;
  is_recalled?: boolean;
  recalled_at?: number;
  recalled_source?: string;
  text: string;
  segments: Array<{ type: string; data: Record<string, unknown> }>;
}

export interface ChatHistoryResponse {
  conversation_id: string;
  chat_type: "group" | "private";
  peer_id: string;
  items: ChatMessageItem[];
  permission?: ChatHistoryPermission;
}

export interface ChatHistoryPermission {
  bot_role: string;
  can_recall: boolean;
  can_set_essence: boolean;
}

export interface ChatInterruptResult {
  cancelled: number;
  skipped_non_interruptible: number;
  skipped_running: number;
  skipped_finished: number;
}

export interface ChatAgentStateItem {
  conversation_id: string;
  pending_count: number;
  running_count: number;
  queued_count: number;
  interruptible_count: number;
  latest_trace_id: string;
  last_trace_id?: string;
  last_user_id?: string;
  last_text_preview?: string;
  last_update?: string;
}

export interface ChatAgentStateResponse {
  items: ChatAgentStateItem[];
  total: number;
}

export const api = new ApiClient();
