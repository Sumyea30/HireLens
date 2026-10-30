// Structured client logger (N22). Entries hold event names and IDs only.
// Keys that could carry personal data or secrets are dropped before storage.
export type Level = "debug" | "info" | "warn" | "error";
export type Ctx = Record<string, string | number | boolean | null>;
export interface LogEntry { ts: string; level: Level; event: string; ctx: Ctx }

const ORDER: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40 };
const MAX_ENTRIES = 1000;
const BLOCKED_KEY = /(email|name|phone|text|token|password|authorization|cv|transcript|message|content|body|file|note|address)/i;

let minLevel: Level = "info";
let entries: LogEntry[] = [];
let pending: LogEntry[] = [];
let sink: ((batch: LogEntry[]) => Promise<void>) | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

export function sanitize(ctx?: Record<string, unknown>): Ctx {
  const out: Ctx = {};
  if (!ctx) return out;
  for (const [key, value] of Object.entries(ctx)) {
    if (BLOCKED_KEY.test(key)) continue;
    if (typeof value === "string") out[key] = value.slice(0, 80);
    else if (typeof value === "number" || typeof value === "boolean" || value === null) out[key] = value;
  }
  return out;
}

function emit(level: Level, event: string, ctx?: Record<string, unknown>) {
  if (ORDER[level] < ORDER[minLevel]) return;
  const entry: LogEntry = { ts: new Date().toISOString(), level, event, ctx: sanitize(ctx) };
  entries.push(entry);
  if (entries.length > MAX_ENTRIES) entries = entries.slice(-MAX_ENTRIES);
  if (sink) pending.push(entry);
  listeners.forEach((fn) => fn());
}

export const log = {
  debug: (event: string, ctx?: Record<string, unknown>) => emit("debug", event, ctx),
  info: (event: string, ctx?: Record<string, unknown>) => emit("info", event, ctx),
  warn: (event: string, ctx?: Record<string, unknown>) => emit("warn", event, ctx),
  error: (event: string, ctx?: Record<string, unknown>) => emit("error", event, ctx),
};

export const getEntries = (): LogEntry[] => entries;
export const setLevel = (level: Level) => { minLevel = level; };
export const getLevel = (): Level => minLevel;
export const clearEntries = () => { entries = []; pending = []; listeners.forEach((fn) => fn()); };
export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

export async function flush(): Promise<void> {
  if (!sink || pending.length === 0) return;
  const batch = pending;
  pending = [];
  try {
    await sink(batch);
  } catch {
    // best effort: shipping failures must never affect the app
  }
}

/** Ship entries to the backend in batches. Pass null to stop. */
export function setSink(fn: ((batch: LogEntry[]) => Promise<void>) | null, intervalMs = 5000) {
  sink = fn;
  if (timer) clearInterval(timer);
  timer = null;
  if (fn) timer = setInterval(() => { void flush(); }, intervalMs);
}

export function toLogText(list: LogEntry[]): string {
  return list.map((e) => `${e.ts} ${e.level.toUpperCase().padEnd(5)} ${e.event} ${JSON.stringify(e.ctx)}`).join("\n");
}

export function downloadText(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
