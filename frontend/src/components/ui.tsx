import { Loader2, SearchX, TriangleAlert, X } from "lucide-react";
import { useEffect, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { api, errorMessage } from "../api";
import { initials, refLabel } from "../lib/format";
import type { EvidenceItem } from "../lib/types";

type Tone = "good" | "info" | "warn" | "bad" | "neutral";

export function Button({ variant = "secondary", loading, children, disabled, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost"; loading?: boolean }) {
  return (
    <button className={`btn btn-${variant}`} disabled={disabled || loading} {...rest}>
      {loading && <Loader2 size={16} className="spin" aria-hidden />}
      {children}
    </button>
  );
}

export const Card = ({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) => (
  <section className={`card ${className}`}>
    {(title || actions) && <header className="card-head"><h2>{title}</h2><div className="row">{actions}</div></header>}
    {children}
  </section>
);

export const Badge = ({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) => (
  <span className={`badge badge-${tone}`}>{children}</span>
);

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: NoInfer<T>; label: string; count?: number }[]; value: T; onChange(v: NoInfer<T>): void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} className={value === t.id ? "tab active" : "tab"} onClick={() => onChange(t.id)}>
          {t.label}{t.count !== undefined && <span className="tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export const Bar = ({ value, tone = "info", max = 100 }: { value: number; tone?: Tone; max?: number }) => (
  <div className="bar" role="progressbar" aria-valuenow={Math.round(value)} aria-valuemin={0} aria-valuemax={max}>
    <div className={`bar-fill bar-${tone}`} style={{ width: `${Math.min(100, Math.max(0, (value / max) * 100))}%` }} />
  </div>
);

export const Spinner = ({ label = "Loading" }: { label?: string }) => (
  <div className="center muted" role="status"><Loader2 className="spin" size={20} aria-hidden /> <span>{label}...</span></div>
);

export const EmptyState = ({ title, text, action }: { title: string; text?: string; action?: ReactNode }) => (
  <div className="empty"><SearchX size={28} aria-hidden /><h3>{title}</h3>{text && <p className="muted">{text}</p>}{action}</div>
);

export const ErrorState = ({ message, onRetry }: { message: string; onRetry?: () => void }) => (
  <div className="error-box" role="alert">
    <TriangleAlert size={20} aria-hidden /><div><strong>Something needs attention</strong><p>{message}</p></div>
    {onRetry && <Button onClick={onRetry}>Try again</Button>}
  </div>
);

export function Modal({ open, title, onClose, children, footer }: { open: boolean; title: string; onClose(): void; children: ReactNode; footer?: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="modal-back" onMouseDown={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(e) => e.stopPropagation()}>
        <header className="card-head"><h2>{title}</h2><button className="icon-btn" onClick={onClose} aria-label="Close"><X size={18} /></button></header>
        <div className="modal-body">{children}</div>
        {footer && <footer className="modal-foot">{footer}</footer>}
      </div>
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div><h1>{title}</h1>{subtitle && <p className="muted">{subtitle}</p>}</div>
      <div className="row no-print">{actions}</div>
    </div>
  );
}

export const Stat = ({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: string; tone?: Tone }) => (
  <div className="card stat"><span className="muted">{label}</span><strong className={tone ? `t-${tone}` : ""}>{value}</strong>{hint && <small className="muted">{hint}</small>}</div>
);

export const Avatar = ({ name }: { name: string }) => <span className="avatar" aria-hidden>{initials(name)}</span>;

/** A reference to evidence. Click to read the CV text, transcript segment or policy it points to. */
export function EvidenceChip({ evidenceRef, preloaded }: { evidenceRef: string; preloaded?: EvidenceItem }) {
  const [open, setOpen] = useState(false);
  const [item, setItem] = useState<EvidenceItem | null>(preloaded ?? null);
  const [err, setErr] = useState<string | null>(null);
  const show = async () => {
    setOpen(true);
    if (item) return;
    try { setItem(await api.getEvidence(evidenceRef)); } catch (e) { setErr(errorMessage(e)); }
  };
  return (
    <>
      <button className="chip" onClick={show} title="Show the evidence">{refLabel(evidenceRef)}</button>
      <Modal open={open} title={refLabel(evidenceRef)} onClose={() => setOpen(false)}>
        {err ? <ErrorState message={err} /> : item ? <blockquote className="quote">{item.text}</blockquote> : <Spinner />}
        <p className="muted small">Reference: {evidenceRef}</p>
      </Modal>
    </>
  );
}

export const Chips = ({ refs }: { refs: string[] }) => (
  <span className="chips">{refs.length ? refs.map((r) => <EvidenceChip key={r} evidenceRef={r} />) : <span className="muted small">No evidence cited</span>}</span>
);
