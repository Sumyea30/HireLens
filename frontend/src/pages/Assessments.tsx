import { GitCompare } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Badge, Button, Card, EmptyState, ErrorState, PageHeader, Spinner } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { formatDate, positionTone, titleCase } from "../lib/format";

export default function Assessments() {
  const nav = useNavigate();
  const { data, error, loading, reload } = useAsync(() => api.listAssessments(), []);
  const [picked, setPicked] = useState<string[]>([]);
  const toggle = (id: string) => setPicked((l) => (l.includes(id) ? l.filter((x) => x !== id) : l.length < 4 ? [...l, id] : l));
  return (
    <>
      <PageHeader title="Assessments" subtitle="Consolidated, evidence-backed reports. Select two or more to compare."
        actions={<Button disabled={picked.length < 2} onClick={() => nav(`/compare?ids=${picked.join(",")}`)}><GitCompare size={16} /> Compare ({picked.length})</Button>} />
      <Card>
        {loading ? <Spinner /> : error ? <ErrorState message={error} onRetry={reload} /> : !data?.length ? (
          <EmptyState title="No assessments yet" text="They appear after an interview ends." action={<Link className="btn" to="/interviews">Go to interviews</Link>} />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th /><th>Candidate</th><th>Job</th><th>Position</th><th className="num">Combined</th><th>Created</th><th /></tr></thead>
            <tbody>{data.map((a) => (
              <tr key={a.id}>
                <td><input type="checkbox" aria-label={`Select ${a.candidate_name}`} checked={picked.includes(a.id)} onChange={() => toggle(a.id)} /></td>
                <td><strong>{a.candidate_name}</strong></td><td>{a.job_title}</td>
                <td><Badge tone={positionTone(a.overall_position)}>{titleCase(a.overall_position)}</Badge>{a.status === "partial" && <> <Badge tone="warn">Partial</Badge></>}</td>
                <td className="num">{a.combined}</td><td>{formatDate(a.created_at)}</td><td className="num"><Link to={`/assessments/${a.id}`}>Open</Link></td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Card>
    </>
  );
}
