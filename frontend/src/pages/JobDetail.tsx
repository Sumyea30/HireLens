import { Upload } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { MAX_UPLOAD_MB, api, errorMessage } from "../api";
import { Badge, Bar, Button, Card, ErrorState, PageHeader, Spinner, Tabs } from "../components/ui";
import { useToast } from "../components/Toast";
import { useAsync } from "../hooks/useAsync";
import { formatDay, normalizeWeights, titleCase } from "../lib/format";
import { log } from "../lib/logger";
import type { FunnelLabel, FunnelReport, UploadResult } from "../lib/types";

export default function JobDetail() {
  const { id = "" } = useParams();
  const { data: job, error, loading, reload } = useAsync(() => api.getJob(id), [id]);
  const [tab, setTab] = useState<"overview" | "weights" | "post" | "cvs" | "funnel">("overview");
  if (loading) return <Spinner />;
  if (error || !job) return <ErrorState message={error ?? "Not found"} onRetry={reload} />;
  return (
    <>
      <PageHeader title={job.title} subtitle={`Created ${formatDay(job.created_at)} · ${job.applicants} applicants`}
        actions={<><Badge tone={job.status === "open" ? "good" : "warn"}>{titleCase(job.status)}</Badge><Link className="btn" to={`/candidates?job=${job.id}`}>View candidates</Link></>} />
      <Card>
        <Tabs tabs={[
          { id: "overview", label: "Requirements" },
          { id: "weights", label: "Scoring weights" },
          { id: "post", label: "Job post" },
          { id: "funnel", label: "Funnel" },
          { id: "cvs", label: "Add CVs" },
        ]} value={tab} onChange={setTab} />
        {tab === "overview" && <Overview job={job} />}
        {tab === "weights" && <Weights jobId={job.id} initial={job.weights} onSaved={reload} />}
        {tab === "post" && (job.post_text ? <><pre className="log-line" style={{ fontFamily: "inherit" }}>{job.post_text}</pre><p className="muted small">Posted to: {job.channels.map(titleCase).join(", ") || "-"}. Applications: {job.apply_email}</p></> : <p className="muted">No post has been published for this job yet.</p>)}
        {tab === "funnel" && <FunnelTab jobId={job.id} />}
        {tab === "cvs" && <Upload_ jobId={job.id} onDone={reload} />}
      </Card>
    </>
  );
}

function funnelLabelTone(label: FunnelLabel): "bad" | "warn" | "good" {
  return label === "bottleneck" ? "bad" : label === "non_discriminating" ? "warn" : "good";
}

function StackedBar({ met, partial, unmet }: { met: number; partial: number; unmet: number }) {
  const total = Math.max(1, met + partial + unmet);
  return (
    <div className="stack-bar" role="img" aria-label={`${met} met, ${partial} partial, ${unmet} unmet`}>
      <span className="stack-seg stack-met" style={{ width: `${(met / total) * 100}%` }} />
      <span className="stack-seg stack-partial" style={{ width: `${(partial / total) * 100}%` }} />
      <span className="stack-seg stack-unmet" style={{ width: `${(unmet / total) * 100}%` }} />
    </div>
  );
}

function FunnelTab({ jobId }: { jobId: string }) {
  const toast = useToast();
  const { data, error, loading, reload } = useAsync(() => api.getFunnel(jobId), [jobId]);
  const [busy, setBusy] = useState(false);
  const [suggestion, setSuggestion] = useState<string | null>(null);
  const suggest = async () => {
    setBusy(true);
    try {
      const r = await api.suggestFunnelRevision(jobId);
      setSuggestion(r.summary);
      toast(r.fed_to_assistant ? "Revision drafted for approval in the job assistant." : "Suggestion ready. Open the job assistant to apply it.", "good");
      if (r.fed_to_assistant) reload();
    } catch (e) {
      toast(errorMessage(e), "bad");
    } finally {
      setBusy(false);
    }
  };
  if (loading) return <Spinner />;
  if (error || !data) return <ErrorState message={error ?? "Not found"} onRetry={reload} />;
  return <FunnelBody report={data} busy={busy} suggestion={suggestion} onSuggest={suggest} />;
}

function FunnelBody({ report, busy, suggestion, onSuggest }: {
  report: FunnelReport; busy: boolean; suggestion: string | null; onSuggest(): void;
}) {
  return (
    <div className="stack">
      <p className="muted small">{report.coverage_note} Shortlist is the top {report.shortlist_n} by official score.</p>
      {report.low_sample && (
        <div className="error-box" role="status">
          <strong>Low sample</strong>
          <p>Only {report.candidate_count} scored candidate{report.candidate_count === 1 ? "" : "s"}. Funnel labels are less reliable below 10.</p>
        </div>
      )}
      <div className="row between">
        <strong>Requirements funnel</strong>
        <div className="row" style={{ gap: 10 }}>
          <span className="muted small"><span className="legend-dot stack-met" /> Met</span>
          <span className="muted small"><span className="legend-dot stack-partial" /> Partial</span>
          <span className="muted small"><span className="legend-dot stack-unmet" /> Unmet</span>
        </div>
      </div>
      {report.requirements.length === 0 ? <p className="muted">No screening requirements yet.</p> : (
        <ul className="list funnel-list">
          {report.requirements.map((r) => (
            <li key={r.requirement_id} className="funnel-row">
              <div className="row between">
                <span>{r.text} {r.mandatory ? <Badge tone="info">mandatory</Badge> : <Badge>preferred</Badge>}</span>
                <Badge tone={funnelLabelTone(r.label)}>{titleCase(r.label.split("_").join(" "))}</Badge>
              </div>
              <StackedBar met={r.met} partial={r.partial} unmet={r.unmet} />
              <p className="muted small">{r.met} met · {r.partial} partial · {r.unmet} unmet · pass rate {(r.pass_rate * 100).toFixed(0)}%</p>
            </li>
          ))}
        </ul>
      )}
      <div>
        <strong>What-if (preferred only)</strong>
        <p className="muted small">If a preferred requirement were treated as met, how the coverage-based shortlist would change. Mandatory requirements are never offered for relaxation.</p>
        {report.what_if.length === 0 ? <p className="muted">No preferred requirements to relax.</p> : (
          <ul className="list">
            {report.what_if.map((w) => (
              <li key={w.requirement_id}>
                <strong>{w.text}</strong> — shortlist size {w.shortlist_size}
                {w.entering.length === 0 ? <span className="muted"> (no new entrants)</span> : (
                  <ul>{w.entering.map((e) => <li key={e.id}><Link to={`/candidates/${e.id}`}>{e.name}</Link> <span className="muted small">est. coverage {(e.coverage_estimate * 100).toFixed(0)}%</span></li>)}</ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <strong>Near misses</strong>
        <p className="muted small">Outside the shortlist, but would enter the coverage top {report.shortlist_n} if exactly one preferred requirement were relaxed.</p>
        {report.near_miss.length === 0 ? <p className="muted">None right now.</p> : (
          <ul className="list">
            {report.near_miss.map((m) => (
              <li key={`${m.candidate_id}-${m.requirement_id}`}>
                <Link to={`/candidates/${m.candidate_id}`}>{m.name}</Link>
                {" "}via <em>{m.requirement_text}</em>
                {m.evidence_refs.length > 0 && <span className="muted small"> · refs {m.evidence_refs.join(", ")}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="row" style={{ gap: 12, alignItems: "center" }}>
        <Button variant="primary" loading={busy} onClick={onSuggest}>Suggest post revision</Button>
        {suggestion && <span className="muted small">{suggestion}</span>}
      </div>
    </div>
  );
}

function Overview({ job }: { job: NonNullable<ReturnType<typeof useAsync<Awaited<ReturnType<typeof api.getJob>>>>["data"]> }) {
  const p = job.profile;
  return (
    <div className="grid-2">
      <div className="stack">
        <div><strong>Required skills</strong><div className="chips">{p.required_skills.map((s) => <Badge key={s} tone="info">{s}</Badge>)}</div></div>
        <div><strong>Preferred skills</strong><div className="chips">{p.preferred_skills.length ? p.preferred_skills.map((s) => <Badge key={s}>{s}</Badge>) : <span className="muted">None</span>}</div></div>
        <div><strong>Experience</strong><p>{p.min_experience_years != null ? `At least ${p.min_experience_years} years` : "-"}</p></div>
        <div><strong>Qualifications</strong><p>{p.qualifications.join(", ") || "-"}</p></div>
        <div><strong>Contact email</strong><p>{p.contact_email || "-"}</p></div>
        <div><strong>Company</strong><p>{p.company || "-"}</p></div>
        <div><strong>Location</strong><p>{p.location_or_mode || p.address || "-"}</p></div>
        <div><strong>Employment type</strong><p>{p.employment_type || "-"}</p></div>
        <div><strong>Experience level</strong><p>{p.experience_level || "-"}</p></div>
        <div><strong>Salary range</strong><p>{p.remuneration || "-"}</p></div>
        <div><strong>Application deadline</strong><p>{p.application_deadline || "-"}</p></div>
        <div><strong>About the role</strong><p>{p.about_role || "-"}</p></div>
      </div>
      <div>
        <strong>Requirements used for screening</strong>
        {job.requirements.length === 0 ? <p className="muted">Requirements appear once the job post is approved.</p> : (
          <ul className="list" style={{ marginTop: 8 }}>{job.requirements.map((r) => <li key={r.id}>{r.text} <Badge tone={r.kind === "required" ? "info" : "neutral"}>{r.kind}</Badge></li>)}</ul>
        )}
      </div>
    </div>
  );
}

function Weights({ jobId, initial, onSaved }: { jobId: string; initial: Record<string, number>; onSaved(): void }) {
  const toast = useToast();
  const [w, setW] = useState<Record<string, number>>(initial);
  const [busy, setBusy] = useState(false);
  const total = Object.values(w).reduce((a, b) => a + b, 0) || 1;
  const save = async () => {
    setBusy(true);
    try { await api.updateWeights(jobId, normalizeWeights(w)); toast("Weights saved. New scores use them.", "good"); onSaved(); } catch (e) { toast(errorMessage(e), "bad"); } finally { setBusy(false); }
  };
  return (
    <div className="stack">
      <p className="muted">Choose how much each dimension counts for this role. Weights are scaled to add up to 100%.</p>
      {Object.entries(w).map(([k, v]) => (
        <div key={k} className="weight-row">
          <label htmlFor={`w-${k}`}>{titleCase(k)}</label>
          <input id={`w-${k}`} type="range" min={0} max={100} value={Math.round(v * 100)} onChange={(e) => setW({ ...w, [k]: Number(e.target.value) / 100 })} />
          <strong className="num">{Math.round((v / total) * 100)}%</strong>
        </div>
      ))}
      <Bar value={Math.round(total * 100)} max={100} tone={Math.abs(total - 1) < 0.01 ? "good" : "warn"} />
      <div><Button variant="primary" loading={busy} onClick={save}>Save weights</Button></div>
    </div>
  );
}

function Upload_({ jobId, onDone }: { jobId: string; onDone(): void }) {
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<UploadResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const send = async (files: File[]) => {
    if (!files.length) return;
    setBusy(true); setError(null);
    try {
      const r = await api.uploadCvs(jobId, files);
      setResults(r);
      log.info("cv_upload", { count: files.length, rejected: r.filter((x) => x.status === "rejected").length });
      onDone();
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  };
  const drop = (e: DragEvent) => { e.preventDefault(); setDrag(false); void send(Array.from(e.dataTransfer.files)); };
  return (
    <div className="stack">
      <div className={`dropzone ${drag ? "drag" : ""}`} onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={drop}>
        <Upload size={26} aria-hidden />
        <strong>Drop CVs here, or choose files</strong>
        <span className="muted small">PDF or DOCX, up to {MAX_UPLOAD_MB} MB each. Use only the provided sample or synthetic CVs.</span>
        <input ref={input} type="file" accept=".pdf,.docx" multiple hidden onChange={(e) => { void send(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
        <Button loading={busy} onClick={() => input.current?.click()}>Choose files</Button>
      </div>
      <p className="muted small">Candidates can also apply by email. Those CVs arrive here automatically. To score a batch and read why each requirement was met or not, use <Link to={`/batch?job=${jobId}`}>Batch review</Link>.</p>
      {error && <ErrorState message={error} />}
      {results.length > 0 && (
        <ul className="list" style={{ listStyle: "none", padding: 0 }}>
          {results.map((r, i) => <li key={i} className="row between"><span>File {i + 1}</span><span className="row">{r.status === "queued" ? <Badge tone="good">Queued for screening</Badge> : <Badge tone="bad">Rejected</Badge>}{r.reason && <small className="muted">{r.reason}</small>}</span></li>)}
        </ul>
      )}
    </div>
  );
}
