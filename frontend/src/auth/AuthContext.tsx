import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { USE_MOCK, api } from "../api";
import { log, setSink } from "../lib/logger";
import type { Role, Session } from "../lib/types";
import { clearSession, getSession, setSession } from "./session";

interface AuthValue {
  session: Session | null;
  role: Role | null;
  login(username: string, password: string): Promise<void>;
  signup(name: string, email: string, password: string): Promise<void>;
  joinWith(s: Session): void;
  logout(): void;
  can(action: "delete" | "manage_policies" | "view_logs"): boolean;
}

const Ctx = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setState] = useState<Session | null>(() => getSession());

  const logout = useCallback(() => {
    log.info("logout");
    clearSession();
    setState(null);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    try {
      const s = await api.login(username, password);
      setSession(s);
      setState(s);
      log.info("login_ok", { role: s.user.role });
    } catch (e) {
      log.warn("login_failed");
      throw e;
    }
  }, []);

  const signup = useCallback(async (name: string, email: string, password: string) => {
    try {
      const s = await api.signup(name, email, password);
      setSession(s);
      setState(s);
      log.info("signup_ok", { role: s.user.role });
    } catch (e) {
      log.warn("signup_failed");
      throw e;
    }
  }, []);

  const joinWith = useCallback((s: Session) => {
    setSession(s);
    setState(s);
    log.info("room_join_ok");
  }, []);

  useEffect(() => {
    const onUnauthorized = () => { log.warn("session_expired"); logout(); };
    window.addEventListener("hl:unauthorized", onUnauthorized);
    return () => window.removeEventListener("hl:unauthorized", onUnauthorized);
  }, [logout]);

  // Ship the client log to the backend while signed in (real mode only).
  useEffect(() => {
    if (USE_MOCK || !session) return;
    setSink((batch) => api.sendClientLogs(batch));
    return () => setSink(null);
  }, [session]);

  const value = useMemo<AuthValue>(() => ({
    session,
    role: session?.user.role ?? null,
    login, signup, joinWith, logout,
    can: (action) => {
      const r = session?.user.role;
      if (action === "delete" || action === "view_logs") return r === "admin";
      if (action === "manage_policies") return r === "admin" || r === "interviewer";
      return false;
    },
  }), [session, login, signup, joinWith, logout]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth must be used inside AuthProvider");
  return v;
}
