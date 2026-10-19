import { Download, FileDown, GitCompare, Printer } from "lucide-react";
import type { CSSProperties } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { Badge, Bar, Button, Card, Chips, ErrorState, PageHeader, Spinner } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { formatDate, positionTone, scoreTone, titleCase } from "../lib/format";
import { downloadText } from "../lib/logger";
import type { Finding } from "../lib/types";

const RING: Record<string, string> = { good: "var(--good)", info: "var(--info)", warn: "var(--warn)", bad: "var(--bad)" };

function Findings({ title, items, empty }: { title: string; items: Finding[]; empty: string }) {
  return (
    <Card title={title}>
      {items.length === 0 ? <p className="muted">{empty}</p> : (
        <ul className="list">{items.map((f, i) => <li key={i}>{f.statement} <Chips refs={f.evidence_refs} /></li>)}</ul>
      )}
    </Card>
  );
}

export default function AssessmentPage() {
  const { id = "" } = useParams();
  const { data: a, error, loading, reload } = useAsync(() => api.getAssessment(id), [id]);
  if (loading) return <Spinner />;
  if (error || !a) return <ErrorState message={error ?? "Not found"} onRetry={reload} />;
  const tone = positionTone(a.overall_position);
  const reqText = (rid: string) => a.requirements.find((r) => r.id === rid)?.text ?? rid;
  return (
    <>
      <PageHeader title={`Assessment: ${a.candidate_name}`} subtitle={`${a.job_title} · generated ${formatDate(a.created_at)}`}
        actions={<>
          <Button onClick={() => window.print()}><Printer size={16} /> Print or save as PDF</Button>
          <Button onClick={() => downloadText(`assessment-${a.id}.json`, JSON.stringify(a, null, 2))}><FileDown size={16} /> Download JSON</Button>
          <Link className="btn" to={`/compare?ids=${a.id}`}><GitCompare size={16} /> Compare</Link>
        </>} />
      {!a.has_interview && <div className="hint">This report is based on CV screening only. No interview results were available.</div>}
      <div className="grid-3">
        <Card title="Overall position">
          <div className="row" style={{ gap: 20 }}>
            <div className="ring" style={{ "--v": a.combined, "--c": RING[scoreTone(a.combined)] } as CSSProperties}><span>{Math.round(a.combined)}</span></div>
            <div><Badge tone={tone}>{titleCase(a.overall_position)}</Badge><p className="muted small" style={{ marginTop: 8 }}>A decision aid. A person makes the hiring decision.</p></div>
          </div>
        </Card>
        <Card title="Component scores">
          <div className="stack">
            <div><div className="row between"><span>CV screening</span><strong>{a.component_scores.cv_score}</strong></div><Bar value={a.component_scores.cv_score} tone={scoreTone(a.component_scores.cv_score)} /></div>
            <div><div className="row between"><span>Interview answers</span><strong>{a.component_scores.interview_score ?? "-"}</strong></div><Bar value={a.component_scores.interview_score ?? 0} tone={scoreTone(a.component_scores.interview_score ?? 0)} /></div>
          </div>
        </Card>
        <Card title="Not yet explored"><p className="muted small">Requirements never discussed in the interview.</p>
          {a.uncovered_requirement_ids.length ? <ul className="list">{a.uncovered_requirement_ids.map((r) => <li key={r}>{reqText(r)}</li>)}</ul> : <p>Everything was covered.</p>}
        </Card>
      </div>
      <div className="grid-2">
        <Findings title="Strengths" items={a.strengths} empty="No strengths with evidence." />
        <Findings title="Weaknesses" items={a.weaknesses} empty="No weaknesses with evidence." />
        <Findings title="Evidence gaps" items={a.evidence_gaps} empty="No evidence gaps found." />
        <Findings title="Suggested follow-up" items={a.follow_up_areas} empty="No follow-up suggested." />
      </div>
      <Card title="Requirement results">
        <div className="table-wrap"><table>
          <thead><tr><th>Requirement</th><th>Result</th><th>Why</th><th>Evidence</th></tr></thead>
          <tbody>{a.requirement_matches.map((m) => (
            <tr key={m.requirement_id}><td>{m.requirement_text}</td><td><Badge tone={m.status === "met" ? "good" : m.status === "partial" ? "warn" : "bad"}>{titleCase(m.status)}</Badge></td><td>{m.justification}</td><td><Chips refs={m.evidence.map((e) => e.ref)} /></td></tr>
          ))}</tbody>
        </table></div>
      </Card>
      {a.qa_evaluations.length > 0 && (
        <Card title="Interview question and answer review">
          <div className="table-wrap"><table>
            <thead><tr><th>Question and answer</th><th className="num">Relevance</th><th className="num">Accuracy</th><th className="num">Complete</th><th className="num">Quality</th><th>Why, with evidence</th></tr></thead>
            <tbody>{a.qa_evaluations.map((q) => (
              <tr key={q.pair_id}>
                <td><strong>{q.question}</strong><div className="muted small">{q.answer}</div></td>
                <td className="num">{q.relevance}</td><td className="num">{q.accuracy}</td><td className="num">{q.completeness}</td><td className="num">{q.quality}</td>
                <td>{q.justification} <Chips refs={q.evidence_refs} /></td>
              </tr>
            ))}</tbody>
          </table></div>
          <p className="muted small"><Download size={12} /> Scores are out of 10.</p>
        </Card>
      )}
    </>
  );
}
