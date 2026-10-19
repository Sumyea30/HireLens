import type { Session } from "../lib/types";

const KEY = "hl_session";

export function getSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}
export function setSession(s: Session): void {
  try { sessionStorage.setItem(KEY, JSON.stringify(s)); } catch { /* storage unavailable */ }
}
export function clearSession(): void {
  try { sessionStorage.removeItem(KEY); } catch { /* storage unavailable */ }
}
