import { Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api";
import { API_BASE } from "../api/client";
import { getSession } from "../auth/session";
import { useAuth } from "../auth/AuthContext";
import { useToast } from "../components/Toast";
import { Badge, Bar, Button, Card, Chips, ErrorState, Modal, PageHeader, Spinner, Tabs } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { formatClock, formatDate, scoreTone, titleCase } from "../lib/format";
import type { MatchStatus } from "../lib/types";

const matchTone = (s: MatchStatus) => (s === "met" ? "good" : s === "partial" ? "warn" : "bad");

export default function CandidateDetail() {
  const { id = "" } = useParams();
  const { can } = useAuth();
  const toast = useToast();
  const nav = useNavigate();
  const { data: c, error, loading, reload } = useAsync(() => api.getCandidate(id), [id]);
  const [tab, setTab] = useState<"scores" | "reqs" | "cv">("scores");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [photoSrc, setPhotoSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!c?.photo_url) { setPhotoSrc(null); return; }
    let objectUrl: string | null = null;
    let cancel = false;
    (async () => {
      const token = getSession()?.token;
      const res = await fetch(`${API_BASE}${c.photo_url}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (!res.ok || cancel) return;
      objectUrl = URL.createObjectURL(await res.blob());
      if (!cancel) setPhotoSrc(objectUrl);
    })().catch(() => undefined);
    return () => { cancel = true; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [c?.photo_url]);
  if (loading) return <Spinner />;
  if (error || !c) return <ErrorState message={error ?? "Not found"} onRetry={reload} />;

  const remove = async () => {
    setBusy(true);
    try { await api.deleteCandidate(c.id); toast("Candidate and all related data deleted.", "good"); nav("/candidates"); }
    catch (e) { toast(errorMessage(e), "bad"); setBusy(false); }
  };

  return (
    <>
      <PageHeader title={can("delete") && c.real_name ? c.real_name : c.name}
        subtitle={c.email ? `${c.email}${c.phone ? ` · ${c.phone}` : ""}` : "No contact details extracted"}
        actions={<>
          <Badge tone={c.status === "failed" ? "bad" : c.status === "incomplete" ? "warn" : "info"}>{titleCase(c.status)}</Badge>
          {(c.assessment_id || c.evaluation) && <Link className="btn" to={`/assessments/${c.assessment_id || c.evaluation?.id}`}>View assessment</Link>}
          {c.status !== "failed" && !c.assessment_id && !c.evaluation && <Link className="btn btn-primary" to="/interviews">Schedule interview</Link>}
          {can("delete") && <Button variant="danger" onClick={() => setConfirm(true)}><Trash2 size={16} /> Delete data</Button>}
        </>} />
      {c.status === "incomplete" && (
        <ErrorState message="This CV is incomplete. Waiting for a reply with a photo and email address." />
      )}
      {can("delete") && photoSrc && (
        <Card title="Candidate photo">
          <img src={photoSrc} alt="" style={{ maxWidth: 160, borderRadius: 8 }} />
        </Card>
      )}
      {c.status === "failed" ? (
        <ErrorState message={c.failure_reason ?? "This CV could not be processed."} />
      ) : c.status === "incomplete" ? null : (
        <>
          <div className="grid-3">
            <Card title="Overall score"><strong style={{ fontSize: "2.4rem" }} className={`t-${scoreTone(c.aggregate_score ?? 0)}`}>{c.aggregate_score ?? "-"}</strong><p className="muted small">Weighted from the dimension scores below.</p></Card>
            <Card title="Requirements met"><strong style={{ fontSize: "2.4rem" }}>{c.matches.filter((m) => m.status === "met").length}/{c.matches.length}</strong><p className="muted small">{c.matches.filter((m) => m.status === "partial").length} partly met</p></Card>
            <Card title="Experience"><strong style={{ fontSize: "2.4rem" }}>{c.parsed.total_experience_years ?? "-"}<small className="muted"> yrs</small></strong><p className="muted small">As stated in the CV</p></Card>
          </div>
          <Card>
            <Tabs tabs={[{ id: "scores", label: "Score breakdown" }, { id: "reqs", label: "Requirements", count: c.matches.length }, { id: "cv", label: "CV summary" }]} value={tab} onChange={setTab} />
            {tab === "scores" && (
              <div className="stack">
                {c.dimension_scores.map((d) => (
                  <div key={d.dimension}>
                    <div className="row between"><strong>{titleCase(d.dimension)}</strong><span className="muted small">weight {Math.round(d.weight * 100)}%</span></div>
                    <div className="score-cell"><strong>{d.score}</strong><Bar value={d.score} tone={scoreTone(d.score)} /></div>
                    <p>{d.justification} <Chips refs={d.evidence_refs} /></p>
                  </div>
                ))}
              </div>
            )}
            {tab === "reqs" && (
              <div className="table-wrap"><table>
                <thead><tr><th>Requirement</th><th>Result</th><th>Why</th><th>Evidence</th></tr></thead>
                <tbody>{c.matches.map((m) => (
                  <tr key={m.requirement_id}>
                    <td>{m.requirement_text}</td><td><Badge tone={matchTone(m.status)}>{titleCase(m.status)}</Badge></td>
                    <td>{m.justification}</td>
                    <td>{m.evidence.length ? m.evidence.map((e) => <div key={e.ref}><Chips refs={[e.ref]} /><div className="muted small">{e.text}</div></div>) : <span className="muted small">None found</span>}</td>
                  </tr>
                ))}</tbody>
              </table></div>
            )}
            {tab === "cv" && (
              <div className="grid-2">
                <div className="stack">
                  <div><strong>Skills</strong><div className="chips">{c.parsed.skills.map((s) => <Badge key={s} tone="info">{s}</Badge>)}</div></div>
                  <div><strong>Education</strong><ul className="list">{c.parsed.education.map((s) => <li key={s}>{s}</li>)}</ul></div>
                  <div><strong>Certifications</strong>{c.parsed.certifications.length ? <ul className="list">{c.parsed.certifications.map((s) => <li key={s}>{s}</li>)}</ul> : <p className="muted">None listed</p>}</div>
                </div>
                <div className="stack">
                  <div><strong>Work history</strong><ul className="list">{c.parsed.work_history.map((s) => <li key={s}>{s}</li>)}</ul></div>
                  <div><strong>Projects</strong><ul className="list">{c.parsed.projects.map((s) => <li key={s}>{s}</li>)}</ul></div>
                  <p className="muted small">Protected details such as age, gender or nationality are removed before scoring and are never used.</p>
                </div>
              </div>
            )}
          </Card>
          <Card title="Interview notes">
            {c.notes.length === 0 ? <p className="muted">No notes yet.</p> : c.notes.map((n) => (
              <div key={n.updated_at ?? n.body}>
                <p>{n.body}</p>
                <p className="muted small">{n.author_name}{n.updated_at ? ` · ${formatDate(n.updated_at)}` : ""}</p>
                {n.markers.length > 0 && <ul className="list">{n.markers.map((m) => <li key={m.at}>Marked at {formatClock(m.offset_ms)}</li>)}</ul>}
              </div>
            ))}
          </Card>
          {c.evaluation && (
            <Card title="Interview evaluation">
              <p><Badge tone="info">{titleCase(c.evaluation.overall_position)}</Badge> Combined {c.evaluation.combined}</p>
              <p className="muted small">{c.evaluation.qa_evaluations.length} answered questions · {c.evaluation.uncovered_requirement_ids.length} requirements not explored</p>
              <Link className="btn" to={`/assessments/${c.evaluation.id}`}>Open the full evaluation</Link>
            </Card>
          )}
        </>
      )}
      <Modal open={confirm} title="Delete this candidate?" onClose={() => setConfirm(false)}
        footer={<><Button onClick={() => setConfirm(false)}>Cancel</Button><Button variant="danger" loading={busy} onClick={remove}>Delete permanently</Button></>}>
        <p>This permanently removes the CV, scores, interview transcript and assessment for this candidate. It cannot be undone.</p>
      </Modal>
    </>
  );
}
