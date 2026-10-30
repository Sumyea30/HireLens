import { useEffect, useState } from "react";

export type ThemePref = "system" | "light" | "dark";
export type ThemeMode = "light" | "dark";
const KEY = "hl_theme";

function apply(pref: ThemePref) {
  const root = document.documentElement;
  if (pref === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", pref);
}

function resolve(pref: ThemePref): ThemeMode {
  if (pref === "light" || pref === "dark") return pref;
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function initTheme() {
  try { apply((localStorage.getItem(KEY) as ThemePref | null) ?? "system"); } catch { /* ignore */ }
}

export function useTheme() {
  const [pref, setPref] = useState<ThemePref>(() => {
    try { return (localStorage.getItem(KEY) as ThemePref | null) ?? "system"; } catch { return "system"; }
  });
  const [mode, setMode] = useState<ThemeMode>(() => resolve(pref));
  useEffect(() => {
    apply(pref);
    setMode(resolve(pref));
    try { localStorage.setItem(KEY, pref); } catch { /* ignore */ }
  }, [pref]);
  useEffect(() => {
    if (pref !== "system") return;
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setMode(mq.matches ? "dark" : "light");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [pref]);
  return { pref, setPref, mode };
}
