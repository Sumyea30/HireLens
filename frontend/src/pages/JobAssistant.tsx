import { CheckCircle2, Send, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api";
import { Badge, Button, Card, Chips, ErrorState, Modal, PageHeader, Spinner, Tabs } from "../components/ui";
import { useToast } from "../components/Toast";
import { useAsync } from "../hooks/useAsync";
import { log } from "../lib/logger";
import type { ApprovalDecision, AssistantReply, ChatMessage } from "../lib/types";
import { titleCase } from "../lib/format";

const WELCOME: ChatMessage = {
  role: "assistant",
  content: "You can fill in the job opening form, or tell me the same details here. I need the title, skills, years of experience, education, contact email, company name, location, employment type, experience level, salary range, application deadline, what the role is about, and the key responsibilities. I will draft the post once those are filled in.",
};
const FIELDS: [string, string][] = [
  ["title", "Job title"], ["company", "Company"], ["location_or_mode", "Location"], ["employment_type", "Employment type"],
  ["experience_level", "Experience level"], ["remuneration", "Salary range"], ["application_deadline", "Application deadline"],
  ["about_role", "About the role"], ["responsibilities", "Key responsibilities"], ["qualifications", "Education"],
  ["min_experience_years", "Years of experience"], ["required_skills", "Technical skills"], ["contact_email", "Contact email"],
];
const EMPLOYMENT = ["Full-time", "Part-time", "Contract", "Internship"];
const LEVELS = ["Entry-level", "Mid-level", "Senior"];
const EMPTY_FORM = {
  title: "", company: "", location: "", employment_type: "", experience_level: "", remuneration: "",
  application_deadline: "", about_role: "", responsibilities: "", education: "", years: "", skills: "",
  communication: "", other: "", preferred: "", offers: "", contact_email: "sumyeac@gmail.com",
};
const APPLY_EMAIL = "sumyeac@gmail.com";

export default function JobAssistant() {
  const { id } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const [state, setState] = useState<AssistantReply | null>(null);
  const [text, setText] = useState("");
  const [template, setTemplate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"profile" | "approval">("profile");
  const [mode, setMode] = useState<"form" | "chat">("form");
  const end = useRef<HTMLDivElement>(null);

  // Skip the fetch when this page created the job itself (no flicker, no lost input).
  const existing = useAsync(async () => (id && state?.job_id !== id ? api.getAssistant(id) : null), [id]);
  useEffect(() => { if (existing.data) { setState(existing.data); if (existing.data.approval) setTab("approval"); } }, [existing.data]);
  useEffect(() => { end.current?.scrollIntoView?.({ behavior: "smooth" }); }, [state?.messages.length, busy]);

  const send = async (message: string) => {
    if (!message.trim() || busy) return;
    setText(""); setBusy(true); setError(null);
    setState((s) => ({ ...(s ?? emptyState()), messages: [...(s?.messages ?? []), { role: "user", content: message }] }));
    try {
      const reply = await api.assistantMessage(state?.job_id ?? id ?? null, message, template);
      setState(reply);
      if (reply.approval) { setTab("approval"); setMode("chat"); }
      if (!id) nav(`/jobs/new/${reply.job_id}`, { replace: true });
    } catch (e) {
      setError(errorMessage(e));
    } finally { setBusy(false); }
  };

  const decide = async (d: ApprovalDecision) => {
    if (!state) return;
    setBusy(true); setError(null);
    try {
      const reply = await api.submitApproval(state.job_id, d);
      setState(reply);
      log.info("post_decision", { action: d.action });
      if (reply.status === "open") toast("Job post published.", "good");
      setTab("profile");
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  };

  if (id && existing.loading && !state) return <Spinner />;
  if (id && existing.error && !state) return <ErrorState message={existing.error} onRetry={existing.reload} />;
  const messages = state?.messages.length ? state.messages : [WELCOME];
  const done = state?.status === "open" || state?.status === "rejected";
  const draftReady = !!state?.approval || state?.status === "awaiting_approval";
  const draftFailed = state?.status === "failed" && !state.approval;

  return (
    <>
      <PageHeader title="Job assistant" subtitle="Fill in the job opening, or describe it in the chat. Then review the draft and approve where it is posted." actions={state && <Badge tone={state.status === "open" ? "good" : state.status === "rejected" ? "bad" : "info"}>{titleCase(state.status)}</Badge>} />
      <div className="split">
        <Card title={mode === "form" ? "Job opening" : "Conversation"}>
          <Tabs tabs={[{ id: "form", label: "Form" }, { id: "chat", label: "Chat" }]} value={mode} onChange={setMode} />
          {error && mode === "form" && <div className="error-box" role="alert" style={{ marginBottom: 8 }}>{error}</div>}
          {draftFailed && mode === "form" && <div className="error-box" role="alert" style={{ marginBottom: 8 }}>The draft could not be prepared. Create draft tries again.</div>}
          {mode === "form" ? <OpeningForm busy={busy || done || draftReady} draftReady={draftReady} onSubmit={(message) => void send(message)} /> : <div className="chat">
            <div className="messages" aria-live="polite">
              {messages.map((m, i) => <div key={i} className={`msg msg-${m.role}`}>{m.content}</div>)}
              {busy && <div className="msg msg-assistant muted">Working on it...</div>}
              <div ref={end} />
            </div>
            <div>
              {error && <div className="error-box" role="alert" style={{ marginBottom: 8 }}>{error}</div>}
              <div className="field" style={{ marginBottom: 8 }}>
                <label htmlFor="job-template">Job description template</label>
                <textarea id="job-template" rows={4} value={template} onChange={(e) => setTemplate(e.target.value)} disabled={done || busy} placeholder={"Optional. Leave empty to use the template stored with your policies.\n\nRole\n{{title}}\n\nRequired skills\n{{required_skills}}"} />
              </div>
              <form className="composer" onSubmit={(e) => { e.preventDefault(); void send(text); }}>
                <input aria-label="Message" placeholder={done ? "This job is finished. Start a new one from the menu." : "For example: We need a backend developer with Python and Docker, 3 years, BSc in CSE, hiring@company.example. Company: what you do. Remuneration: the pay. Start date: 1 November 2026. Address: the office."} value={text} onChange={(e) => setText(e.target.value)} disabled={done || busy} />
                <Button variant="primary" type="submit" disabled={done || !text.trim()} loading={busy}><Send size={16} /> Send</Button>
              </form>
            </div>
          </div>}
        </Card>
        <Card>
          <Tabs tabs={[{ id: "profile", label: "Job profile" }, { id: "approval", label: "Draft and approval", count: state?.approval ? 1 : undefined }]} value={tab} onChange={setTab} />
          {tab === "profile" ? <ProfilePanel state={state} /> : <ApprovalPanel state={state} busy={busy} onDecide={decide} />}
        </Card>
      </div>
    </>
  );
}

const emptyState = (): AssistantReply => ({ job_id: "", status: "collecting", messages: [], profile: {}, missing_fields: [], approval: null, publish_results: [], apply_email: null });

function linesOf(value: string): string {
  return value.split(/\n|;/).map((part) => part.trim()).filter(Boolean).join("; ");
}

function openingMessage(form: typeof EMPTY_FORM): string {
  const rows: [string, string][] = [
    ["Job title", form.title], ["Company", form.company], ["Location", form.location],
    ["Employment type", form.employment_type], ["Experience level", form.experience_level],
    ["Salary range", form.remuneration], ["Application deadline", form.application_deadline],
    ["About the role", form.about_role], ["Key responsibilities", linesOf(form.responsibilities)],
    ["Education", form.education], ["Experience", form.years ? `${form.years} years` : ""],
    ["Technical skills", form.skills], ["Communication", form.communication],
    ["Other requirements", form.other], ["Preferred qualifications", linesOf(form.preferred)],
    ["What we offer", linesOf(form.offers)],
    ["Contact", form.contact_email],
  ];
  return rows.filter(([, value]) => value.trim()).map(([label, value]) => `${label}: ${value.trim()}`).join("\n");
}

function OpeningForm({ busy, draftReady, onSubmit }: { busy: boolean; draftReady?: boolean; onSubmit(message: string): void }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [problem, setProblem] = useState<string | null>(null);
  const set = (key: keyof typeof EMPTY_FORM) => (event: { target: { value: string } }) => setForm((current) => ({ ...current, [key]: event.target.value }));
  const submit = (event: { preventDefault(): void }) => {
    event.preventDefault();
    const required: [keyof typeof EMPTY_FORM, string][] = [
      ["title", "job title"], ["company", "company"], ["location", "location"], ["employment_type", "employment type"],
      ["experience_level", "experience level"], ["remuneration", "salary range"], ["application_deadline", "application deadline"],
      ["about_role", "about the role"], ["responsibilities", "key responsibilities"], ["education", "education"],
      ["years", "years of experience"], ["skills", "technical skills"], ["contact_email", "contact email"],
    ];
    const missing = required.filter(([key]) => !form[key].trim()).map(([, label]) => label);
    if (missing.length) {
      setProblem(`Fill in ${missing.join(", ")}.`);
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.contact_email.trim())) {
      setProblem("Enter a contact email like hiring@company.com.");
      return;
    }
    setProblem(null);
    onSubmit(openingMessage(form));
  };
  return (
    <form className="job-form" onSubmit={submit}>
      <p className="muted note">{draftReady ? "A draft is already ready. Review it under Draft and approval." : "Fill this form, or switch to Chat and describe the same opening."}</p>
      {problem && <div className="error-box" role="alert">{problem}</div>}
      <div className="grid-2">
        <div className="field"><label htmlFor="job-title">Job title</label><input id="job-title" value={form.title} onChange={set("title")} disabled={busy} /></div>
        <div className="field"><label htmlFor="company">Company</label><input id="company" value={form.company} onChange={set("company")} disabled={busy} /></div>
        <div className="field"><label htmlFor="location">Location</label><input id="location" placeholder="City, Country / Remote / Hybrid" value={form.location} onChange={set("location")} disabled={busy} /></div>
        <div className="field"><label htmlFor="employment-type">Employment type</label>
          <select id="employment-type" value={form.employment_type} onChange={set("employment_type")} disabled={busy}>
            <option value="">Choose one</option>
            {EMPLOYMENT.map((item) => <option key={item}>{item}</option>)}
          </select>
        </div>
        <div className="field"><label htmlFor="experience-level">Experience level</label>
          <select id="experience-level" value={form.experience_level} onChange={set("experience_level")} disabled={busy}>
            <option value="">Choose one</option>
            {LEVELS.map((item) => <option key={item}>{item}</option>)}
          </select>
        </div>
        <div className="field"><label htmlFor="salary">Salary range</label><input id="salary" placeholder="Salary range or Negotiable" value={form.remuneration} onChange={set("remuneration")} disabled={busy} /></div>
        <div className="field"><label htmlFor="deadline">Application deadline</label><input id="deadline" placeholder="1 November 2026" value={form.application_deadline} onChange={set("application_deadline")} disabled={busy} /></div>
        <div className="field"><label htmlFor="years">Years of experience</label><input id="years" inputMode="numeric" value={form.years} onChange={set("years")} disabled={busy} /></div>
      </div>
      <div className="field"><label htmlFor="about-role">About the role</label><textarea id="about-role" rows={3} value={form.about_role} onChange={set("about_role")} disabled={busy} placeholder="What the successful candidate will contribute." /></div>
      <div className="field"><label htmlFor="responsibilities">Key responsibilities</label><textarea id="responsibilities" rows={4} value={form.responsibilities} onChange={set("responsibilities")} disabled={busy} placeholder={"One responsibility on each line."} /></div>
      <h3>Required qualifications</h3>
      <div className="grid-2">
        <div className="field"><label htmlFor="education">Education</label><input id="education" placeholder="Required degree or equivalent experience" value={form.education} onChange={set("education")} disabled={busy} /></div>
        <div className="field"><label htmlFor="skills">Technical skills</label><input id="skills" placeholder="Python, Docker" value={form.skills} onChange={set("skills")} disabled={busy} /></div>
        <div className="field"><label htmlFor="communication">Communication</label><input id="communication" value={form.communication} onChange={set("communication")} disabled={busy} /></div>
        <div className="field"><label htmlFor="other">Other requirements</label><input id="other" value={form.other} onChange={set("other")} disabled={busy} /></div>
      </div>
      <div className="field"><label htmlFor="preferred">Preferred qualifications</label><textarea id="preferred" rows={3} value={form.preferred} onChange={set("preferred")} disabled={busy} placeholder={"One preferred skill or certification on each line."} /></div>
      <div className="field"><label htmlFor="offers">What we offer</label><textarea id="offers" rows={3} value={form.offers} onChange={set("offers")} disabled={busy} placeholder={"Salary and benefits, learning, and flexibility. One item on each line."} /></div>
      <h3>How to apply</h3>
      <div className="field"><label htmlFor="contact-email">Contact</label><input id="contact-email" type="email" value={APPLY_EMAIL} readOnly /></div>
      <p className="muted note">Applications arrive by email at this address after the post is published.</p>
      <p className="muted note">Equal opportunity statement: We welcome applications from qualified candidates and evaluate applicants based on job-related qualifications and evidence of relevant skills.</p>
      <div><Button variant="primary" type="submit" loading={busy} disabled={busy}>Create draft</Button></div>
    </form>
  );
}

function ProfilePanel({ state }: { state: AssistantReply | null }) {
  const p = state?.profile ?? {};
  const val = (k: string): string => {
    const v = (p as Record<string, unknown>)[k];
    return Array.isArray(v) ? v.join(", ") : v == null ? "" : String(v);
  };
  return (
    <div className="stack">
      <div>{FIELDS.map(([k, label]) => (
        <div key={k} className="field-state"><span>{label}</span>{val(k) ? <strong>{val(k)}</strong> : <Badge tone="warn">Needed</Badge>}</div>
      ))}</div>
      {state?.status === "open" && (
        <div className="hint">
          <CheckCircle2 size={16} /> Published. Candidates apply by email to <strong>{state.apply_email}</strong> with the job code in the subject.{" "}
          <Link to={`/jobs/${state.job_id}`}>Open the job</Link>
        </div>
      )}
      {state?.status === "rejected" && <div className="error-box"><XCircle size={18} /> The draft was rejected. Nothing was published.</div>}
    </div>
  );
}

function ApprovalPanel({ state, busy, onDecide }: { state: AssistantReply | null; busy: boolean; onDecide(d: ApprovalDecision): void }) {
  const a = state?.approval;
  const [post, setPost] = useState("");
  const [channels, setChannels] = useState<string[]>([]);
  const [feedback, setFeedback] = useState("");
  const [asking, setAsking] = useState(false);
  useEffect(() => { if (a) { setPost(a.draft_post); setChannels(a.proposed_channels.slice(0, 1)); } }, [a?.draft_post]);
  if (!a) return <p className="muted">No draft to approve yet. Keep describing the role in the conversation.</p>;
  const edited = post !== a.draft_post;
  return (
    <div className="stack">
      <div className="field"><label htmlFor="post">Draft job post (you can edit it)</label><textarea id="post" rows={9} value={post} onChange={(e) => setPost(e.target.value)} /></div>
      {a.mandatory_failures.length > 0 && <div className="error-box">{a.mandatory_failures.join(" ")}</div>}
      <div>
        <strong>Checks against company policy</strong>
        {a.compliance_findings.map((f, i) => (
          <div key={i} className="finding"><Badge tone={f.severity === "blocker" ? "bad" : f.severity === "warning" ? "warn" : "good"}>{f.severity}</Badge><div>{f.message}<div><Chips refs={f.policy_refs} /></div></div></div>
        ))}
        {a.unresolved_items.length > 0 && <p className="muted small">Left for you to decide: {a.unresolved_items.join(", ")}.</p>}
      </div>
      <div>
        <strong>Where should it be posted?</strong>
        {a.proposed_channels.map((c) => (
          <label key={c} className="check" style={{ padding: "6px 0" }}>
            <input type="checkbox" checked={channels.includes(c)} onChange={(e) => setChannels((l) => (e.target.checked ? [...l, c] : l.filter((x) => x !== c)))} />
            <span>{titleCase(c)}</span>
          </label>
        ))}
        {a.proposed_channels.length === 0 && (
          <div className="error-box" role="alert">No posting channel is set up, so this post cannot be published yet. Ask the administrator to configure the Blogger connection (MCP).</div>
        )}
        <p className="muted small">Nothing is published until you approve.</p>
      </div>
      <div className="row">
        <Button variant="primary" loading={busy} disabled={!channels.length} onClick={() => onDecide({ action: "approve", channels, edited_post: edited ? post : null, feedback: null })}>Approve and publish</Button>
        <Button onClick={() => setAsking(true)}>Request changes</Button>
        <Button variant="danger" onClick={() => onDecide({ action: "reject", channels: [], edited_post: null, feedback: null })}>Reject</Button>
      </div>
      <Modal open={asking} title="What should change?" onClose={() => setAsking(false)}
        footer={<><Button onClick={() => setAsking(false)}>Cancel</Button><Button variant="primary" disabled={!feedback.trim()} onClick={() => { onDecide({ action: "changes", channels, edited_post: null, feedback }); setAsking(false); setFeedback(""); }}>Send</Button></>}>
        <textarea aria-label="Requested changes" value={feedback} onChange={(e) => setFeedback(e.target.value)} placeholder="For example: make the tone warmer and shorten the introduction." />
      </Modal>
    </div>
  );
}
