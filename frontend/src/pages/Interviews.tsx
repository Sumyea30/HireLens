import { Copy, DoorOpen, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorMessage } from "../api";
import { useToast } from "../components/Toast";
import { Badge, Button, Card, EmptyState, ErrorState, Modal, PageHeader, Spinner } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { formatDate, titleCase } from "../lib/format";
import type { Interview } from "../lib/types";

const tone = (s: Interview["status"]) => (s === "completed" ? "good" : s === "live" ? "info" : s === "interrupted" ? "bad" : "neutral");

export default function Interviews() {
  const toast = useToast();
  const nav = useNavigate();
  const { data, error, loading, reload } = useAsync(() => api.listInterviews(), []);
  const jobs = useAsync(() => api.listJobs(), []);
  const cands = useAsync(() => api.listCandidates(), []);
  const [open, setOpen] = useState(false);
  const [jobId, setJobId] = useState("");
  const [candId, setCandId] = useState("");
  const [when, setWhen] = useState("");
  const [moveId, setMoveId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const eligible = useMemo(() => (cands.data ?? []).filter((c) => c.job_id === jobId && ["screened", "interview_scheduled", "interviewed"].includes(c.status)), [cands.data, jobId]);

  const link = (i: Interview) => `${location.origin}/join/${i.join_token}`;
  const staffLink = (i: Interview) => `${location.origin}/interviews/${i.id}/room`;
  const copy = async (i: Interview) => { try { await navigator.clipboard.writeText(link(i)); toast("Candidate link copied.", "good"); } catch { toast(link(i)); } };
  const copyStaff = async (i: Interview) => { try { await navigator.clipboard.writeText(staffLink(i)); toast("Interviewer link copied.", "good"); } catch { toast(staffLink(i)); } };
  const move = async () => {
    if (!moveId) return;
    setBusy("move");
    try {
      const i = await api.rescheduleInterview(moveId, localStamp(when));
      toast(i.email_sent ? "The new time was emailed." : (i.email_error || "The time changed, but the email was not sent."), i.email_sent ? "good" : "bad");
      setMoveId(null); await copy(i); reload();
    } catch (e) { toast(errorMessage(e), "bad"); } finally { setBusy(null); }
  };
  const create = async () => {
    setBusy("create");
    try {
      const i = await api.createInterview(jobId, candId, localStamp(when));
      const interviewer = i.interviewer_email_sent ? "The interviewer was emailed too." : (i.interviewer_email_error || "Copy the interviewer link from this page.");
      toast(i.email_sent ? `The candidate was emailed. ${interviewer}` : (i.email_error || "The interview was created, but the email was not sent."), i.email_sent && i.interviewer_email_sent !== false ? "good" : "bad");
      setOpen(false); await copy(i); reload();
    }
    catch (e) { toast(errorMessage(e), "bad"); } finally { setBusy(null); }
  };
  const assess = async (i: Interview) => {
    setBusy(i.id);
    try { const a = await api.generateAssessment(i.id); nav(`/assessments/${a.id}`); } catch (e) { toast(errorMessage(e), "bad"); setBusy(null); }
  };

  return (
    <>
      <PageHeader title="Interviews" subtitle="Schedule a time. The candidate and the interviewer are each emailed a room link and a calendar invitation." actions={<Button variant="primary" onClick={() => setOpen(true)}><Plus size={16} /> New interview</Button>} />
      <Card>
        {loading ? <Spinner /> : error ? <ErrorState message={error} onRetry={reload} /> : !data?.length ? <EmptyState title="No interviews yet" text="Create one for a screened candidate." /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Candidate</th><th>Job</th><th>When</th><th>Status</th><th /></tr></thead>
            <tbody>{data.map((i) => (
              <tr key={i.id}>
                <td><strong>{i.candidate_name}</strong></td><td>{i.job_title}</td><td>{i.scheduled_at ? formatDate(i.scheduled_at) : formatDate(i.created_at)}</td><td><Badge tone={tone(i.status)}>{titleCase(i.status)}</Badge></td>
                <td><div className="row" style={{ justifyContent: "flex-end" }}>
                  {i.status !== "completed" && <>{i.join_token && <Button onClick={() => copy(i)}><Copy size={14} /> Candidate link</Button>}<Button onClick={() => copyStaff(i)}><Copy size={14} /> Interviewer link</Button><Button onClick={() => { setMoveId(i.id); setWhen(""); }}>Reschedule</Button><Button variant="primary" onClick={() => nav(`/interviews/${i.id}/room`)}><DoorOpen size={14} /> Open room</Button></>}
                  {i.status === "completed" && <Button loading={busy === i.id} onClick={() => assess(i)}>View assessment</Button>}
                </div></td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </Card>
      <Modal open={open} title="New interview" onClose={() => setOpen(false)}
        footer={<><Button onClick={() => setOpen(false)}>Cancel</Button><Button variant="primary" disabled={!jobId || !candId || !when} loading={busy === "create"} onClick={create}>Schedule and email</Button></>}>
        <div className="field"><label htmlFor="ij">Job</label><select id="ij" value={jobId} onChange={(e) => { setJobId(e.target.value); setCandId(""); }}><option value="">Choose a job</option>{(jobs.data ?? []).filter((j) => j.status === "open").map((j) => <option key={j.id} value={j.id}>{j.title}</option>)}</select></div>
        <div className="field"><label htmlFor="ic">Candidate</label><select id="ic" value={candId} onChange={(e) => setCandId(e.target.value)} disabled={!jobId}><option value="">Choose a candidate</option>{eligible.map((c) => <option key={c.id} value={c.id}>{c.name} (score {c.aggregate_score})</option>)}</select></div>
        <div className="field"><label htmlFor="when">Date and time</label><input id="when" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} /></div>
        <p className="muted small">The candidate and the interviewer are each emailed their own room link, in plain text and HTML, with a calendar invitation. The interview lasts 60 minutes. If an email cannot be sent, the interview is still scheduled and the link stays here to copy.</p>
      </Modal>
      <Modal open={!!moveId} title="Reschedule interview" onClose={() => setMoveId(null)}
        footer={<><Button onClick={() => setMoveId(null)}>Cancel</Button><Button variant="primary" disabled={!when} loading={busy === "move"} onClick={move}>Save and email</Button></>}>
        <div className="field"><label htmlFor="move-when">New date and time</label><input id="move-when" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} /></div>
      </Modal>
    </>
  );
}

function localStamp(value: string): string {
  const date = new Date(value);
  const pad = (n: number) => String(Math.abs(n)).padStart(2, "0");
  const off = -date.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  return `${value.length === 16 ? `${value}:00` : value}${sign}${pad(Math.floor(Math.abs(off) / 60))}:${pad(Math.abs(off) % 60)}`;
}
