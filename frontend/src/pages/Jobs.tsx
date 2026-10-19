import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Card, EmptyState, ErrorState, PageHeader, Spinner } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { formatDay, titleCase } from "../lib/format";
import type { JobStatus } from "../lib/types";

const tone = (s: JobStatus) => (s === "open" ? "good" : s === "awaiting_approval" ? "warn" : s === "rejected" || s === "failed" ? "bad" : "neutral");

export default function Jobs() {
  const { data, error, loading, reload } = useAsync(() => api.listJobs(), []);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("all");
  const rows = useMemo(() => (data ?? []).filter((j) => (status === "all" || j.status === status) && j.title.toLowerCase().includes(q.toLowerCase())), [data, q, status]);
  return (
    <>
      <PageHeader title="Jobs" subtitle="Roles you are hiring for." actions={<Link className="btn btn-primary" to="/jobs/new"><Plus size={16} /> New job</Link>} />
      <Card>
        <div className="row" style={{ marginBottom: 14 }}>
          <input aria-label="Search jobs" placeholder="Search by title" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 260 }} />
          <select aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)} style={{ maxWidth: 200 }}>
            <option value="all">All statuses</option>
            {(["draft", "awaiting_approval", "open", "closed", "rejected", "failed"] as const).map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}
          </select>
        </div>
        {loading ? <Spinner /> : error ? <ErrorState message={error} onRetry={reload} /> : rows.length === 0 ? <EmptyState title="No jobs found" text="Try a different search or create a new job." /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Title</th><th>Status</th><th className="num">Applicants</th><th>Created</th><th /></tr></thead>
            <tbody>{rows.map((j) => (
              <tr key={j.id}>
                <td><strong>{j.title}</strong></td><td><Badge tone={tone(j.status)}>{titleCase(j.status)}</Badge></td>
                <td className="num">{j.applicants}</td><td>{formatDay(j.created_at)}</td>
                <td className="num">{j.status === "awaiting_approval" || j.status === "draft" ? <Link to={`/jobs/new/${j.id}`}>Continue</Link> : <Link to={`/jobs/${j.id}`}>Open</Link>}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Card>
    </>
  );
}
