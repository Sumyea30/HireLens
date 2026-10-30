import {
  BarChart3, Briefcase, CalendarClock, FileText, Files, LayoutDashboard, LogOut, Menu, ScrollText,
  Settings, ShieldCheck, Sparkles, Users,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { USE_MOCK } from "../api";
import { useAuth } from "../auth/AuthContext";
import { initials } from "../lib/format";
import { log } from "../lib/logger";

interface Item { to: string; label: string; icon: ReactNode; end?: boolean; logs?: boolean }
const ITEMS: Item[] = [
  { to: "/", label: "Dashboard", icon: <LayoutDashboard size={18} />, end: true },
  { to: "/jobs/new", label: "Job assistant", icon: <Sparkles size={18} /> },
  { to: "/jobs", label: "Jobs", icon: <Briefcase size={18} />, end: true },
  { to: "/candidates", label: "Candidates", icon: <Users size={18} /> },
  { to: "/batch", label: "Batch review", icon: <Files size={18} /> },
  { to: "/interviews", label: "Interviews", icon: <CalendarClock size={18} /> },
  { to: "/assessments", label: "Assessments", icon: <BarChart3 size={18} /> },
  { to: "/policies", label: "Policies", icon: <FileText size={18} /> },
  { to: "/logs", label: "Activity log", icon: <ScrollText size={18} />, logs: true },
  { to: "/settings", label: "Settings", icon: <Settings size={18} /> },
];

export function Layout() {
  const { session, logout, can } = useAuth();
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  const nav = useNavigate();

  useEffect(() => {
    log.info("page_view", { path: loc.pathname });
    setOpen(false);
  }, [loc.pathname]);

  return (
    <div className={`app ${open ? "nav-open" : ""}`}>
      <aside className="sidebar no-print">
        <div className="brand"><span className="logo" aria-hidden /> <strong>HireLens</strong></div>
        <nav aria-label="Main">
          {ITEMS.filter((i) => !i.logs || can("view_logs")).map((i) => (
            <NavLink key={i.to} to={i.to} end={i.end} className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
              {i.icon}<span>{i.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          {USE_MOCK && <div className="demo-note"><ShieldCheck size={14} /> Demo mode: synthetic data</div>}
          <div className="user">
            <span className="avatar">{initials(session?.user.name ?? "")}</span>
            <div><strong>{session?.user.name}</strong><small className="muted">{session?.user.role}</small></div>
            <button className="icon-btn" aria-label="Sign out" onClick={() => { logout(); nav("/login"); }}><LogOut size={18} /></button>
          </div>
        </div>
      </aside>
      <div className="main">
        <header className="topbar no-print">
          <button className="icon-btn menu-btn" aria-label="Menu" onClick={() => setOpen((o) => !o)}><Menu size={20} /></button>
          <span className="muted small">Evidence-backed hiring decisions. People decide, HireLens explains.</span>
        </header>
        <main className="page"><Outlet /></main>
      </div>
    </div>
  );
}
