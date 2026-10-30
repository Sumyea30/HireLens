import { Moon, Sun } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Bar, Card, EmptyState, ErrorState, PageHeader, Spinner, Stat } from "../components/ui";
import { useAuth } from "../auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { useTheme } from "../hooks/useTheme";
import { formatDate, titleCase } from "../lib/format";

export default function Dashboard() {
  const { can } = useAuth();
  const { mode, setPref } = useTheme();
  const { data, error, loading, reload } = useAsync(async () => {
    try { await api.checkMail(); } catch { /* inbox optional */ }
    return api.getDashboard();
  }, []);
  const max = data ? Math.max(1, ...data.pipeline.map((p) => p.count)) : 1;
  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="Where your hiring stands today."
        actions={
          <div className="row">
            <div className="theme-toggle" role="group" aria-label="Color theme">
              <button
                type="button"
                className={`btn ${mode === "light" ? "btn-primary" : ""}`}
                aria-pressed={mode === "light"}
                onClick={() => setPref("light")}
              >
                <Sun size={16} aria-hidden /> Light
              </button>
              <button
                type="button"
                className={`btn ${mode === "dark" ? "btn-primary" : ""}`}
                aria-pressed={mode === "dark"}
                onClick={() => setPref("dark")}
              >
                <Moon size={16} aria-hidden /> Dark
              </button>
            </div>
            <Link className="btn btn-primary" to="/jobs/new">New job with assistant</Link>
          </div>
        }
      />
      {loading && <Spinner />}
      {!loading && (error || !data) && <ErrorState message={error ?? "No data"} onRetry={reload} />}
      {!loading && data && (
        <>
          <div className="grid-4">
            <Stat label="Open jobs" value={data.open_jobs} />
            <Stat label="Applications" value={data.applications_total} hint={`${data.awaiting_screening} being screened`} />
            <Stat label="Interviews scheduled" value={data.interviews_scheduled} />
            <Stat label="Assessments ready" value={data.assessments_ready} tone="good" />
          </div>
          {data.failed_cvs > 0 && (
            <div className="error-box" role="alert">
              <div><strong>{data.failed_cvs} CV could not be read</strong><p>Scanned or damaged files are skipped so the rest of the batch continues. <Link to="/candidates">Review them</Link>.</p></div>
            </div>
          )}
          <div className="split">
            <Card title="Hiring pipeline">
              <div className="stack">
                {data.pipeline.map((p) => (
                  <div key={p.stage} className="score-cell" style={{ gridTemplateColumns: "110px 1fr 32px" }}>
                    <span>{p.stage}</span><Bar value={p.count} max={max} tone="info" /><strong className="num">{p.count}</strong>
                  </div>
                ))}
              </div>
            </Card>
            {can("view_logs") && (
              <Card title="Recent activity" actions={<Link to="/logs">All activity</Link>}>
                {data.recent.length === 0 ? <EmptyState title="Nothing yet" /> : (
                  <ul className="list" style={{ listStyle: "none", padding: 0 }}>
                    {data.recent.map((e) => (
                      <li key={e.id} className="row between">
                        <span>{titleCase(e.action)} <span className="muted small">({e.entity_id})</span></span>
                        <span className="row"><Badge tone={e.outcome === "ok" ? "good" : e.outcome === "denied" ? "warn" : "bad"}>{e.outcome}</Badge><small className="muted">{formatDate(e.ts)}</small></span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            )}
          </div>
        </>
      )}
    </>
  );
}
