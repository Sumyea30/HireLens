import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Bar, Card, EmptyState, ErrorState, PageHeader, Spinner } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { positionTone, scoreTone, titleCase } from "../lib/format";

export default function Compare() {
  const [params] = useSearchParams();
  const ids = (params.get("ids") ?? "").split(",").filter(Boolean);
  const { data, error, loading, reload } = useAsync(() => Promise.all(ids.map((i) => api.getAssessment(i))), [ids.join(",")]);
  if (!ids.length) return <EmptyState title="Nothing to compare" text="Select assessments from the list." action={<Link className="btn" to="/assessments">Go to assessments</Link>} />;
  if (loading) return <Spinner />;
  if (error || !data) return <ErrorState message={error ?? "No data"} onRetry={reload} />;
  const reqs = data[0].requirements;
  return (
    <>
      <PageHeader title="Compare candidates" subtitle={`${data[0].job_title}: side by side on the same requirements.`} />
      <Card>
        <div className="table-wrap"><table className="compare">
          <thead><tr><th>&nbsp;</th>{data.map((a) => <th key={a.id}><Link to={`/assessments/${a.id}`}>{a.candidate_name}</Link></th>)}</tr></thead>
          <tbody>
            <tr><td><strong>Overall position</strong></td>{data.map((a) => <td key={a.id}><Badge tone={positionTone(a.overall_position)}>{titleCase(a.overall_position)}</Badge></td>)}</tr>
            <tr><td><strong>Combined score</strong></td>{data.map((a) => <td key={a.id}><div className="score-cell"><strong>{a.combined}</strong><Bar value={a.combined} tone={scoreTone(a.combined)} /></div></td>)}</tr>
            <tr><td>CV screening</td>{data.map((a) => <td key={a.id}>{a.component_scores.cv_score}</td>)}</tr>
            <tr><td>Interview answers</td>{data.map((a) => <td key={a.id}>{a.component_scores.interview_score ?? "-"}</td>)}</tr>
            {reqs.map((r) => (
              <tr key={r.id}><td>{r.text}</td>{data.map((a) => {
                const m = a.requirement_matches.find((x) => x.requirement_id === r.id);
                return <td key={a.id}>{m ? <Badge tone={m.status === "met" ? "good" : m.status === "partial" ? "warn" : "bad"}>{titleCase(m.status)}</Badge> : "-"}</td>;
              })}</tr>
            ))}
            <tr><td><strong>Main strength</strong></td>{data.map((a) => <td key={a.id}>{a.strengths[0]?.statement ?? "-"}</td>)}</tr>
            <tr><td><strong>Main concern</strong></td>{data.map((a) => <td key={a.id}>{a.weaknesses[0]?.statement ?? "-"}</td>)}</tr>
            <tr><td><strong>Not yet explored</strong></td>{data.map((a) => <td key={a.id}>{a.uncovered_requirement_ids.length ? `${a.uncovered_requirement_ids.length} requirement(s)` : "None"}</td>)}</tr>
          </tbody>
        </table></div>
        <p className="muted small" style={{ marginTop: 12 }}>This comparison supports a decision. It does not make one.</p>
      </Card>
    </>
  );
}
