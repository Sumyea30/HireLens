import { Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { MAX_UPLOAD_MB, api, errorMessage } from "../api";
import { Badge, Bar, Button, Card, EmptyState, ErrorState, PageHeader, Spinner } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { scoreTone, titleCase } from "../lib/format";
import type { CvReview, MatchStatus } from "../lib/types";

const matchTone = (s: MatchStatus) => (s === "met" ? "good" : s === "partial" ? "warn" : "bad");
const matchLabel = (s: MatchStatus) => (s === "met" ? "Met" : s === "partial" ? "Partially met" : "Not met");

export default function BatchReview() {
  const [params, setParams] = useSearchParams();
  const jobs = useAsync(() => api.listJobs(), []);
  const openJobs = useMemo(() => (jobs.data ?? []).filter((j) => j.status === "open"), [jobs.data]);
  const requested = params.get("job") ?? "";
  const [jobId, setJobId] = useState(requested);
  const [review, setReview] = useState<CvReview | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [loadingReview, setLoadingReview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [drag, setDrag] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!jobId && openJobs.length) setJobId(openJobs[0].id);
  }, [jobId, openJobs]);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    setLoadingReview(true);
    setReviewError(null);
    api.getCvReview(jobId).then(
      (data) => { if (!cancelled) { setReview(data); setLoadingReview(false); } },
      (e) => { if (!cancelled) { setReview(null); setReviewError(errorMessage(e)); setLoadingReview(false); } },
    );
    return () => { cancelled = true; };
  }, [jobId]);

  const job = openJobs.find((j) => j.id === jobId);
  const send = async (files: File[]) => {
    if (!jobId || !files.length) return;
    setBusy(true);
    setUploadError(null);
    try {
      const result = await api.batchCvs(jobId, files);
      setReview(result.review);
      const rejected = result.files.filter((f) => f.status !== "screened");
      if (rejected.length) setUploadError(rejected.map((f) => `${f.filename}: ${f.reason ?? "Could not be scored."}`).join(" "));
    } catch (e) {
      setUploadError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const drop = (e: DragEvent) => { e.preventDefault(); setDrag(false); void send(Array.from(e.dataTransfer.files)); };
  const chooseJob = (id: string) => { setJobId(id); setParams(id ? { job: id } : {}); };

  return (
    <>
      <PageHeader title="Batch review" subtitle="Upload CVs for a job you can see. Gemini scores them and explains each requirement." />
      {jobs.loading ? <Spinner /> : jobs.error ? <ErrorState message={jobs.error} onRetry={jobs.reload} /> : openJobs.length === 0 ? (
        <EmptyState title="No open jobs" text="A job has to be open before CVs can be scored." action={<Link className="btn" to="/jobs">Go to jobs</Link>} />
      ) : (
        <>
          <Card>
            <div className="stack">
              <div className="field" style={{ maxWidth: 360 }}>
                <label htmlFor="batch-job">Job</label>
                <select id="batch-job" value={jobId} onChange={(e) => chooseJob(e.target.value)}>
                  {openJobs.map((j) => <option key={j.id} value={j.id}>{j.title}</option>)}
                </select>
              </div>
              {job && job.requirements.length === 0 ? (
                <p className="muted">This job has no requirements yet, so Gemini has nothing to score against. Approve the job post first.</p>
              ) : (
                <div className={`dropzone ${drag ? "drag" : ""}`} onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={drop}>
                  <Upload size={26} aria-hidden />
                  <strong>Drop several CVs here, or choose files</strong>
                  <span className="muted small">PDF or DOCX, up to {MAX_UPLOAD_MB} MB each. The files are scored together. The same file scored again keeps the same score.</span>
                  <input ref={input} type="file" accept=".pdf,.docx" multiple hidden onChange={(e) => { void send(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
                  <Button variant="primary" loading={busy} onClick={() => input.current?.click()}>Score these CVs</Button>
                </div>
              )}
              {uploadError && <ErrorState message={uploadError} />}
            </div>
          </Card>
          {loadingReview ? <Spinner /> : reviewError ? <ErrorState message={reviewError} /> : review && (
            <>
              <Card title="Why Gemini ranked them this way">
                <p className="review-why">{review.why_these}</p>
              </Card>
              {review.candidates.length === 0 ? (
                <EmptyState title="No scores yet" text="Upload CVs for this job and Gemini will rank them here." />
              ) : (
                <Card title={`${review.job_title}: scores`}>
                  <div className="stack">
                    {review.candidates.map((c) => (
                      <article key={c.id} className="stack">
                        <div className="row between">
                          <div>
                            <strong>#{c.rank} </strong>
                            <Link to={`/candidates/${c.id}`}><strong>{c.name}</strong></Link>
                          </div>
                          <div className="score-cell"><strong>{c.aggregate_score}</strong><Bar value={c.aggregate_score} tone={scoreTone(c.aggregate_score)} /></div>
                        </div>
                        <p>{c.why}</p>
                        <div className="row">
                          <Badge tone="good">{c.met} met</Badge>
                          <Badge tone="warn">{c.partial} partially met</Badge>
                          <Badge tone="bad">{c.unmet} not met</Badge>
                        </div>
                        <div className="table-wrap"><table>
                          <thead><tr><th>Requirement</th><th>Result</th><th>Why Gemini decided that</th></tr></thead>
                          <tbody>{c.matches.map((m) => (
                            <tr key={m.requirement_id}>
                              <td>{m.requirement_text}</td>
                              <td><Badge tone={matchTone(m.status)}>{matchLabel(m.status)}</Badge></td>
                              <td>{m.justification}</td>
                            </tr>
                          ))}</tbody>
                        </table></div>
                        {c.dimensions.length > 0 && (
                          <div className="stack">
                            {c.dimensions.map((d) => (
                              <div key={d.dimension}>
                                <div className="row between"><strong>{titleCase(d.dimension)}</strong><span>{d.score}</span></div>
                                <p className="muted small">{d.justification}</p>
                              </div>
                            ))}
                          </div>
                        )}
                      </article>
                    ))}
                  </div>
                </Card>
              )}
              {review.unscored.length > 0 && (
                <Card title="Not scored">
                  <ul className="list">{review.unscored.map((c) => <li key={c.id}><strong>{c.name}</strong> — {c.reason}</li>)}</ul>
                </Card>
              )}
            </>
          )}
        </>
      )}
    </>
  );
}
