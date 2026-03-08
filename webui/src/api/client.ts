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

  getPrompts() {
    return this.request<{ content: string; parsed: Record<string, unknown> }>("/prompts");
  }

  updatePrompts(content: string) {
    return this.request<{ ok: boolean; message: string }>("/prompts", {
      method: "PUT",
      body: JSON.stringify({ content }),
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

  logsStreamUrl(): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/api/webui/logs/stream?token=${this.getToken()}`;
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

export const api = new ApiClient();
