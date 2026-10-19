import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Bar, Card, EmptyState, ErrorState, PageHeader, Spinner, Stat } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { formatDate, titleCase } from "../lib/format";

export default function Dashboard() {
  const { data, error, loading, reload } = useAsync(() => api.getDashboard(), []);
  if (loading) return <Spinner />;
  if (error || !data) return <ErrorState message={error ?? "No data"} onRetry={reload} />;
  const max = Math.max(1, ...data.pipeline.map((p) => p.count));
  return (
    <>
      <PageHeader title="Dashboard" subtitle="Where your hiring stands today." actions={<Link className="btn btn-primary" to="/jobs/new">New job with assistant</Link>} />
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
      </div>
    </>
  );
}
