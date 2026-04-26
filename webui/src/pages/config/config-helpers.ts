import type { Cfg, FieldDef } from "./config-schema";

export function getPath(obj: Cfg, path: string): unknown {
  return path.split(".").reduce((o: unknown, k) => (o && typeof o === "object" ? (o as Cfg)[k] : undefined), obj);
}

export function setPath(obj: Cfg, path: string, value: unknown): Cfg {
  const keys = path.split(".");
  const result = { ...obj };
  let node: Cfg = result;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i];
    node[k] = { ...(typeof node[k] === "object" && node[k] ? (node[k] as Cfg) : {}) };
    node = node[k] as Cfg;
  }
  node[keys[keys.length - 1]] = value;
  return result;
}

export function parseListValue(input: string): string[] {
  return input.split(/[\n,，]/g).map((s) => s.trim()).filter(Boolean);
}

export function parseGroupVerbosityMap(input: string): Record<string, string> {
  const map: Record<string, string> = {};
  const alias: Record<string, string> = { "详细": "verbose", "中等": "medium", "简洁": "brief", "极简": "minimal" };
  const allowed = new Set(["verbose", "medium", "brief", "minimal"]);
  const lines = input.split(/\r?\n/g);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const m = line.match(/^(\d{5,20})\s*[:=，,\s]\s*(\S+)$/);
    if (!m) continue;
    const gid = m[1];
    const rawVerb = m[2].trim();
    const normalized = (alias[rawVerb] || rawVerb.toLowerCase()).trim();
    if (!allowed.has(normalized)) continue;
    map[gid] = normalized;
  }
  return map;
}

export function formatGroupVerbosityMap(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const rows = Object.entries(value as Record<string, unknown>)
    .filter(([k, v]) => !!k && !!v)
    .map(([k, v]) => [String(k), String(v).toLowerCase()] as const)
    .filter(([, v]) => ["verbose", "medium", "brief", "minimal"].includes(v))
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  return rows.map(([gid, verbosity]) => `${gid}=${verbosity}`).join("\n");
}

export function parseGroupTextMap(input: string): Record<string, string> {
  const map: Record<string, string> = {};
  const lines = input.split(/\r?\n/g);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    let gid = "";
    let text = "";
    const pair = line.match(/^(\d{5,20})\s*(?:=|:|：|，|,)\s*(.+)$/);
    if (pair) {
      gid = pair[1];
      text = pair[2].trim();
    } else {
      const ws = line.match(/^(\d{5,20})\s+(.+)$/);
      if (!ws) continue;
      gid = ws[1];
      text = ws[2].trim();
    }
    if (!gid || !text) continue;
    map[gid] = text;
  }
  return map;
}

export function formatGroupTextMap(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const rows = Object.entries(value as Record<string, unknown>)
    .map(([gid, v]) => [String(gid).trim(), String(v ?? "").trim()] as const)
    .filter(([gid, text]) => /^\d{5,20}$/.test(gid) && !!text)
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  return rows.map(([gid, text]) => `${gid}=${text}`).join("\n");
}

export function parseTextMap(input: string): Record<string, string> {
  const map: Record<string, string> = {};
  const lines = input.split(/\r?\n/g);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const pair = line.match(/^([^=:=：，,]+?)\s*(?:=|:|：|，|,)\s*(.+)$/);
    if (!pair) continue;
    const key = String(pair[1] || "").trim();
    const value = String(pair[2] || "").trim();
    if (!key || !value) continue;
    map[key] = value;
  }
  return map;
}

export function formatTextMap(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  return Object.entries(value as Record<string, unknown>)
    .map(([key, v]) => [String(key).trim(), String(v ?? "").trim()] as const)
    .filter(([key, text]) => !!key && !!text)
    .sort((a, b) => a[0].localeCompare(b[0], "zh-CN"))
    .map(([key, text]) => `${key}=${text}`)
    .join("\n");
}

export function parseNumberInput(raw: string, current: unknown, field: FieldDef): number {
  const text = raw.trim();
  const fallback = typeof current === "number"
    ? current
    : (typeof field.min === "number" ? field.min : 0);
  if (text === "" || text === "-" || text === "." || text === "-.") return fallback;
  const n = Number.parseFloat(text);
  if (!Number.isFinite(n)) return fallback;
  let next = n;
  if (typeof field.min === "number") next = Math.max(field.min, next);
  if (typeof field.max === "number") next = Math.min(field.max, next);
  return next;
}

export function mergeDefaults(def: unknown, cur: unknown): unknown {
  if (Array.isArray(def)) return Array.isArray(cur) ? cur : [...def];
  if (def && typeof def === "object") {
    const base = def as Cfg;
    const current = (cur && typeof cur === "object" && !Array.isArray(cur)) ? (cur as Cfg) : {};
    const out: Cfg = {};
    Object.keys(base).forEach((k) => { out[k] = mergeDefaults(base[k], current[k]); });
    Object.keys(current).forEach((k) => { if (!(k in out)) out[k] = current[k]; });
    return out;
  }
  return cur === undefined ? def : cur;
}

export function withDefaults(raw: Cfg): Cfg {
  const merged = mergeDefaults(DEFAULT_CONFIG, raw);
  return merged && typeof merged === "object" && !Array.isArray(merged) ? (merged as Cfg) : { ...DEFAULT_CONFIG };
}

export const DEFAULT_CONFIG: Cfg = {};
