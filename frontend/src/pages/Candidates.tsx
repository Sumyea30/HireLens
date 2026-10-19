import { GitCompare, Loader2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Bar, Button, Card, EmptyState, ErrorState, PageHeader, Spinner } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { formatDay, scoreTone, titleCase } from "../lib/format";
import type { Candidate, CandidateStatus } from "../lib/types";

const statusTone = (s: CandidateStatus) =>
  s === "failed" ? "bad" : s === "assessed" ? "good" : s === "queued" || s === "processing" ? "warn" : s === "screened" ? "info" : "neutral";
const BUSY: CandidateStatus[] = ["queued", "processing"];

export default function Candidates() {
  const [params, setParams] = useSearchParams();
  const job = params.get("job") ?? "";
  const nav = useNavigate();
  const jobs = useAsync(() => api.listJobs(), []);
  const { data, error, loading, reload } = useAsync(() => api.listCandidates(job || undefined), [job], {
    pollMs: 3000, pollWhile: (d) => d.some((c) => BUSY.includes(c.status)),
  });
  const [picked, setPicked] = useState<string[]>([]);

  const ranked = useMemo(() => {
    const list = [...(data ?? [])];
    list.sort((a, b) => (b.aggregate_score ?? -1) - (a.aggregate_score ?? -1));
    return list;
  }, [data]);
  const pending = ranked.filter((c) => BUSY.includes(c.status)).length;
  const toggle = (c: Candidate) => setPicked((l) => (l.includes(c.id) ? l.filter((x) => x !== c.id) : l.length < 4 ? [...l, c.id] : l));
  const assessmentIds = picked.map((id) => ranked.find((c) => c.id === id)?.assessment_id).filter((x): x is string => !!x);

  return (
    <>
      <PageHeader title="Candidates" subtitle="Ranked by suitability against the job requirements."
        actions={<Button disabled={assessmentIds.length < 2} onClick={() => nav(`/compare?ids=${assessmentIds.join(",")}`)}><GitCompare size={16} /> Compare ({assessmentIds.length})</Button>} />
      <Card>
        <div className="row" style={{ marginBottom: 14 }}>
          <select aria-label="Filter by job" value={job} onChange={(e) => setParams(e.target.value ? { job: e.target.value } : {})} style={{ maxWidth: 260 }}>
            <option value="">All jobs</option>
            {(jobs.data ?? []).map((j) => <option key={j.id} value={j.id}>{j.title}</option>)}
          </select>
          {pending > 0 && <span className="row muted"><Loader2 size={16} className="spin" /> Screening {pending} CV{pending > 1 ? "s" : ""}...</span>}
          {picked.length > 0 && assessmentIds.length < 2 && <span className="muted small">Comparison needs two or more candidates with an assessment.</span>}
        </div>
        {loading ? <Spinner /> : error ? <ErrorState message={error} onRetry={reload} /> : ranked.length === 0 ? (
          <EmptyState title="No candidates yet" text="Add CVs to a job, or wait for applications by email." action={<Link className="btn" to="/jobs">Go to jobs</Link>} />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th /><th className="num">Rank</th><th>Candidate</th><th>Score</th><th>Status</th><th>Source</th><th>Received</th></tr></thead>
            <tbody>{ranked.map((c, i) => (
              <tr key={c.id}>
                <td><input type="checkbox" aria-label={`Select ${c.name}`} checked={picked.includes(c.id)} onChange={() => toggle(c)} /></td>
                <td className="num">{c.aggregate_score != null ? i + 1 : "-"}</td>
                <td><Link to={`/candidates/${c.id}`}><strong>{c.name}</strong></Link></td>
                <td>{c.aggregate_score != null ? <div className="score-cell"><strong>{c.aggregate_score}</strong><Bar value={c.aggregate_score} tone={scoreTone(c.aggregate_score)} /></div> : <span className="muted">-</span>}</td>
                <td><Badge tone={statusTone(c.status)}>{BUSY.includes(c.status) && <Loader2 size={12} className="spin" />}{titleCase(c.status)}</Badge></td>
                <td>{titleCase(c.source)}</td><td>{formatDay(c.received_at)}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Card>
    </>
  );
}
