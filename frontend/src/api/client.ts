import { getSession } from "../auth/session";
import { log } from "../lib/logger";

export const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? "/api";
export const WS_BASE: string = import.meta.env.VITE_WS_BASE ?? "/ws";
export const MAX_UPLOAD_MB = Number(import.meta.env.VITE_MAX_UPLOAD_MB ?? 10);

/** Errors shown to users are always generic (N12, N25). A reference ID helps support. */
export class ApiError extends Error {
  status: number;
  userMessage: string;
  reference?: string;
  constructor(status: number, userMessage: string, reference?: string) {
    super(userMessage);
    this.status = status;
    this.userMessage = userMessage;
    this.reference = reference;
  }
}

async function readDetail(res: Response): Promise<string | null> {
  try {
    const body = await res.json();
    return typeof body?.detail === "string" && body.detail.length > 0 && body.detail.length < 300 ? body.detail : null;
  } catch {
    return null;
  }
}

function friendly(status: number): string {
  if (status === 0) return "Cannot reach the server. Check your connection and try again.";
  if (status === 400 || status === 422) return "Some of the information looks invalid. Please check it and try again.";
  if (status === 401) return "Your session has expired or the details are wrong. Please sign in again.";
  if (status === 403) return "You do not have permission to do that.";
  if (status === 404) return "We could not find what you asked for.";
  if (status === 413) return "That file is too large.";
  if (status === 429) return "Too many requests. Please wait a moment and try again.";
  return "Something went wrong on our side. Please try again. If it keeps happening, contact the administrator.";
}

export async function request<T>(method: string, path: string, body?: unknown, form?: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getSession()?.token;
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload: BodyInit | undefined;
  if (form) payload = form;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { method, headers, body: payload });
  } catch {
    log.error("api_unreachable", { method, path: path.split("?")[0] });
    throw new ApiError(0, friendly(0));
  }
  if (!res.ok) {
    const reference = res.headers.get("x-correlation-id") ?? undefined;
    log.warn("api_error", { method, path: path.split("?")[0], status: res.status, reference: reference ?? null });
    const detail = res.status === 409 || res.status === 429 || res.status === 502 ? await readDetail(res) : null;
    if (res.status === 401 && !path.startsWith("/auth/") && !path.startsWith("/join/")) {
      window.dispatchEvent(new Event("hl:unauthorized"));
    }
    throw new ApiError(res.status, detail ?? friendly(res.status), reference);
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}
