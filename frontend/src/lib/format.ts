import type { Position } from "./types";

export const formatDate = (iso: string | null): string =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "-";

export const formatDay = (iso: string | null): string =>
  iso ? new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" }) : "-";

export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60).toString().padStart(2, "0");
  const s = (total % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

export const initials = (name: string): string =>
  name.split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0]?.toUpperCase() ?? "").join("");

export const clamp = (n: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, n));

export const titleCase = (s: string): string =>
  s.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/** Evidence refs: cv:<candidate>:<chunk>, tr:<session>:<turn>, pol:<doc>:<chunk>, req:<id> */
export function refLabel(ref: string): string {
  const [kind, a, b] = ref.split(":");
  if (kind === "cv") return `CV ${b ?? a}`;
  if (kind === "tr") return `Transcript ${b ?? a}`;
  if (kind === "pol") return `Policy ${a}${b ? ` ${b}` : ""}`;
  if (kind === "req") return `Requirement ${a}`;
  return ref;
}

export const positionTone = (p: Position): "good" | "info" | "warn" | "bad" =>
  ({ strong: "good", promising: "info", uncertain: "warn", weak: "bad" } as const)[p];

export const scoreTone = (n: number): "good" | "info" | "warn" | "bad" =>
  n >= 75 ? "good" : n >= 60 ? "info" : n >= 45 ? "warn" : "bad";

export const normalizeWeights = (w: Record<string, number>): Record<string, number> => {
  const total = Object.values(w).reduce((a, b) => a + b, 0);
  if (total <= 0) return w;
  return Object.fromEntries(Object.entries(w).map(([k, v]) => [k, Math.round((v / total) * 100) / 100]));
};
