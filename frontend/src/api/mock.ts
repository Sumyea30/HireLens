// In-browser demo backend. Everything here is synthetic and resets on reload.
import { getSession } from "../auth/session";
import type {
  ApprovalDecision, Assessment, AssistantReply, AuditEvent, Candidate, CopilotReply, CvBatchResult, CvReview,
  InterviewNote, JobProfile, Policy, RequirementMatch, Role, UploadResult,
} from "../lib/types";
import { ApiError, MAX_UPLOAD_MB } from "./client";
import type { Api } from "./contract";
import {
  EVIDENCE_TEXT, POLICY_EVIDENCE, REQUIREMENTS, TRANSCRIPT, buildAssessment, buildDetail, buildMatches, seedAudit, seedCandidates,
  seedInterviews, seedJobs, seedPolicies,
} from "./mockData";

const delay = (ms = 220) => new Promise<void>((r) => setTimeout(r, ms));
const now = () => new Date().toISOString();

const db = {
  jobs: seedJobs(),
  candidates: seedCandidates(),
  interviews: seedInterviews(),
  assessments: [] as Assessment[],
  policies: seedPolicies(),
  audit: seedAudit(),
  assistants: new Map<string, AssistantReply>(),
  notes: new Map<string, InterviewNote>(),
  copilotUsed: new Map<string, number>(),
  evalRuns: new Map<string, number>(),
};
db.assessments = [
  buildAssessment("asm-1", db.candidates[0], "int-1", "strong", 80),
  buildAssessment("asm-2", db.candidates[1], "int-2", "promising", 70),
];

const MANDATORY_LABELS: Record<string, string> = {
  title: "the job title", required_skills: "the required skills",
  min_experience_years: "the minimum years of experience", qualifications: "the qualifications",
  contact_email: "a contact email",
  company: "the company name", location_or_mode: "the location", employment_type: "the employment type",
  experience_level: "the experience level", remuneration: "the salary range",
  application_deadline: "the application deadline", about_role: "about the role",
  responsibilities: "the key responsibilities",
};
const KNOWN_SKILLS = ["python", "django", "fastapi", "docker", "sql", "postgresql", "react", "aws", "kubernetes", "machine learning", "excel", "tableau", "power bi"];
const fileScores = new Map<string, number>();

function stableScore(name: string, size: number): number {
  const key = `${name}:${size}`;
  const known = fileScores.get(key);
  if (known != null) return known;
  let hash = 0;
  for (let i = 0; i < key.length; i++) hash = (hash * 33 + key.charCodeAt(i)) >>> 0;
  const score = 45 + (hash % 51);
  fileScores.set(key, score);
  return score;
}

function explainCv(name: string, score: number, matches: RequirementMatch[]): string {
  const met = matches.filter((m) => m.status === "met");
  const partial = matches.filter((m) => m.status === "partial");
  const unmet = matches.filter((m) => m.status === "unmet");
  const clause = (rows: RequirementMatch[], label: string) =>
    rows.length ? `${label}: ${rows.map((m) => `${m.requirement_text}: ${m.justification}`).join(" ")}` : "";
  return [
    `${name} scored ${score}.`,
    `Gemini marked ${met.length} met, ${partial.length} partially met and ${unmet.length} not met.`,
    clause(met, "Met"),
    clause(partial, "Partially met"),
    clause(unmet, "Not met"),
  ].filter(Boolean).join(" ");
}

function reviewFor(jobId: string): CvReview {
  const job = db.jobs.find((j) => j.id === jobId);
  const ranked = db.candidates
    .filter((c) => c.job_id === jobId && c.aggregate_score != null && c.status !== "failed")
    .map((c) => {
      const matches = buildMatches(c.id, c.aggregate_score ?? 0, job?.requirements?.length ? job.requirements : REQUIREMENTS);
      return {
        id: c.id, name: c.name, aggregate_score: c.aggregate_score ?? 0,
        why: explainCv(c.name, c.aggregate_score ?? 0, matches),
        met: matches.filter((m) => m.status === "met").length,
        partial: matches.filter((m) => m.status === "partial").length,
        unmet: matches.filter((m) => m.status === "unmet").length,
        matches: matches.map(({ evidence: _evidence, ...rest }) => rest),
        dimensions: buildDetail(c).dimension_scores.map(({ dimension, score, justification }) => ({ dimension, score, justification })),
        rank: 0,
      };
    })
    .sort((a, b) => b.aggregate_score - a.aggregate_score || a.id.localeCompare(b.id));
  ranked.forEach((c, i) => { c.rank = i + 1; });
  const unscored = db.candidates
    .filter((c) => c.job_id === jobId && (c.aggregate_score == null || c.status === "failed"))
    .map((c) => ({ id: c.id, name: c.name, status: c.status, reason: c.status === "failed" ? "The file could not be read." : "Scoring has not finished." }));
  const why = !ranked.length
    ? "No CV has been scored for this job yet."
    : [
      "Gemini scored every CV against the same requirements and the same weights. Uploading the same file again reuses that judgment, so the score stays the same.",
      `Gemini ranked ${ranked[0].name} first. ${ranked[0].why}`,
      ...ranked.slice(1).map((c) => `${c.name} is next. ${c.why}`),
    ].join("\n\n");
  return { job_id: jobId, job_title: job?.title ?? "Job", why_these: why, candidates: ranked, unscored };
}

function audit(action: string, entity: string, entityId: string, outcome: AuditEvent["outcome"] = "ok") {
  const actor = (getSession()?.user.role ?? "system") as Role | "system";
  db.audit.unshift({ id: `ev-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`, ts: now(), actor_role: actor, action, entity, entity_id: entityId, outcome });
}

function missingFields(p: Partial<JobProfile>): string[] {
  const out: string[] = [];
  if (!p.title) out.push("title");
  if (!p.required_skills?.length) out.push("required_skills");
  if (p.min_experience_years == null) out.push("min_experience_years");
  if (!p.qualifications?.length) out.push("qualifications");
  if (!p.contact_email) out.push("contact_email");
  if (!p.company) out.push("company");
  if (!p.location_or_mode) out.push("location_or_mode");
  if (!p.employment_type) out.push("employment_type");
  if (!p.experience_level) out.push("experience_level");
  if (!p.remuneration) out.push("remuneration");
  if (!p.application_deadline) out.push("application_deadline");
  if (!p.about_role) out.push("about_role");
  if (!p.responsibilities?.length) out.push("responsibilities");
  return out;
}

const DEFAULT_TEMPLATE = [
  "Job description template. This is the job post wording to follow.",
  "",
  "# Job Opening: {{title}}",
  "",
  "**Company:** {{company}}",
  "**Location:** {{location_or_mode}}",
  "**Employment Type:** {{employment_type}}",
  "**Experience Level:** {{experience_level}}",
  "**Salary Range:** {{remuneration}}",
  "**Application Deadline:** {{application_deadline}}",
  "",
  "## About the Role",
  "{{about_role}}",
  "",
  "## Key Responsibilities",
  "{{responsibilities}}",
  "",
  "## Required Qualifications",
  "* Education: {{qualifications}}",
  "* Experience: At least {{min_experience_years}} years of relevant experience.",
  "* Technical Skills: {{required_skills}}",
  "* Communication: {{communication}}",
  "* Other Requirements: {{other_criteria}}",
  "",
  "## Preferred Qualifications",
  "{{preferred_skills}}",
  "",
  "## What We Offer",
  "{{offers}}",
  "",
  "## How to Apply",
  "Email your CV to {{contact_email}} by **{{application_deadline}}**.",
  "",
  "**Equal Opportunity Statement:** We welcome applications from qualified candidates and evaluate applicants based on job-related qualifications and evidence of relevant skills.",
  "",
  "**Contact:** {{contact_email}}",
].join("\n");
let storedTemplate: string | null = null;

function draftPost(p: Partial<JobProfile>, template?: string): string {
  const skills = (p.required_skills ?? []).join(", ");
  const source = (template || storedTemplate || DEFAULT_TEMPLATE);
  if (template || storedTemplate) {
    const fill: [string, string][] = [
      ["{{title}}", p.title ?? "New role"],
      ["{{required_skills}}", skills],
      ["{{min_experience_years}}", String(p.min_experience_years ?? "-")],
      ["{{qualifications}}", (p.qualifications ?? []).join(", ")],
      ["{{contact_email}}", p.contact_email ?? ""],
      ["{{company}}", p.company ?? ""],
      ["{{remuneration}}", p.remuneration ?? ""],
      ["{{start_date}}", p.start_date ?? ""],
      ["{{address}}", p.address ?? ""],
      ["{{location_or_mode}}", p.location_or_mode ?? ""],
      ["{{employment_type}}", p.employment_type ?? ""],
      ["{{experience_level}}", p.experience_level ?? ""],
      ["{{application_deadline}}", p.application_deadline ?? ""],
      ["{{about_role}}", p.about_role ?? ""],
      ["{{responsibilities}}", (p.responsibilities ?? []).join("\n")],
      ["{{communication}}", p.communication ?? ""],
      ["{{other_criteria}}", (p.other_criteria ?? []).join(", ")],
      ["{{preferred_skills}}", (p.preferred_skills ?? []).join(", ")],
      ["{{offers}}", (p.offers ?? []).join("\n")],
      ["{{apply_documents}}", p.apply_documents ?? ""],
    ];
    return fill.reduce((text, [key, value]) => text.split(key).join(value), source);
  }
  return [
    p.title ?? "New role", "",
    `We are looking for a ${p.title ?? "colleague"} to join our team.`,
    `Required skills: ${skills}.`,
    `Experience: at least ${p.min_experience_years ?? "-"} years.`,
    `Qualifications: ${(p.qualifications ?? []).join(", ")}.`,
    `Company: ${p.company ?? ""}.`,
    `Location: ${p.location_or_mode ?? ""}.`,
    `Employment type: ${p.employment_type ?? ""}.`,
    `Experience level: ${p.experience_level ?? ""}.`,
    `Salary range: ${p.remuneration ?? ""}.`,
    `Application deadline: ${p.application_deadline ?? ""}.`,
    `About the role: ${p.about_role ?? ""}.`,
    `Contact: ${p.contact_email ?? ""}.`, "",
    "We are an equal opportunity employer.",
  ].join("\n");
}

function approvalFor(p: Partial<JobProfile>): NonNullable<AssistantReply["approval"]> {
  return {
    draft_post: draftPost(p),
    proposed_channels: ["blogger"],
    policy_evidence: POLICY_EVIDENCE,
    compliance_findings: [
      { severity: "info", message: "Title and skill listing follow the posting standards.", policy_refs: ["pol:pol-1:c001"] },
      { severity: "warning", message: "No salary band is available for this role. It was left out for the approver to decide.", policy_refs: [] },
    ],
    mandatory_failures: [], unresolved_items: ["Salary band"],
  };
}

function ensureAssistant(jobId: string): AssistantReply {
  const st = db.assistants.get(jobId);
  if (!st) throw new ApiError(404, "We could not find what you asked for.");
  return st;
}
db.assistants.set("job-2", {
  job_id: "job-2", status: "awaiting_approval",
  messages: [
    { role: "user", content: "We need a data analyst with SQL and data visualisation, 2 years, bachelor's degree." },
    { role: "assistant", content: "I drafted a job post using your company policies. Please review it and choose where to post it." },
  ],
  profile: db.jobs[1].profile, missing_fields: [], approval: approvalFor(db.jobs[1].profile), publish_results: [], apply_email: null,
});

function parseInto(profile: Partial<JobProfile>, text: string): Partial<JobProfile> {
  const next = { ...profile };
  const lower = text.toLowerCase();
  if (!next.title) {
    const m = /(?:need|hire|hiring|looking for)\s+(?:an?\s+)?([a-z ]{3,40}?)(?:\.|,|$| with| who| for)/i.exec(text);
    next.title = (m?.[1] ?? "New role").trim().replace(/\b\w/g, (c) => c.toUpperCase());
  }
  const found = KNOWN_SKILLS.filter((s) => lower.includes(s)).map((s) => s.replace(/\b\w/g, (c) => c.toUpperCase()));
  if (found.length) next.required_skills = Array.from(new Set([...(next.required_skills ?? []), ...found]));
  const years = /(\d+)\s*\+?\s*years?/i.exec(text);
  if (years) next.min_experience_years = Number(years[1]);
  const qual = /(b\.?sc|bachelor|m\.?sc|master|diploma|degree)[^,.;]*/i.exec(text);
  if (qual) next.qualifications = Array.from(new Set([...(next.qualifications ?? []), qual[0].trim()]));
  const email = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/.exec(text);
  if (email) next.contact_email = email[0].toLowerCase();
  const blocks = [...text.matchAll(/(?:^|\n)\s*([A-Za-z][A-Za-z ]{2,40})\s*[:\-]\s*([^\n]+)/g)];
  const take = (names: string[]) => blocks.find((m) => names.includes(m[1].trim().toLowerCase()))?.[2]?.trim().replace(/[.\s]+$/, "");
  const list = (value?: string) => (value ?? "").split(/\s*(?:;|,)\s*/).map((part) => part.trim()).filter(Boolean);
  const titled = take(["job title", "title"]);
  const company = take(["company"]);
  const location = take(["location", "address"]);
  const employment = take(["employment type"]);
  const level = take(["experience level"]);
  const salary = take(["salary range", "remuneration", "compensation", "salary", "pay"]);
  const deadline = take(["application deadline", "deadline"]);
  const starts = take(["starting date", "start date"]);
  const about = take(["about the role"]);
  const duties = take(["key responsibilities", "responsibilities"]);
  const education = take(["education", "qualifications"]);
  const experience = take(["experience"]);
  const skills = take(["technical skills", "required skills", "skills"]);
  const communication = take(["communication"]);
  const other = take(["other requirements"]);
  const preferred = take(["preferred qualifications", "preferred skills"]);
  const offers = take(["what we offer"]);
  const documents = take(["required documents", "documents"]);
  const contact = take(["contact email", "recruitment email", "contact"]);
  if (titled) next.title = titled;
  if (company) next.company = company;
  if (location) next.location_or_mode = location;
  if (employment) next.employment_type = employment;
  if (level) next.experience_level = level;
  if (salary) next.remuneration = salary;
  if (deadline) next.application_deadline = deadline;
  if (starts) next.start_date = starts;
  if (about) next.about_role = about;
  if (duties) next.responsibilities = list(duties);
  if (education) next.qualifications = list(education);
  if (experience) {
    const years = /(\d+)/.exec(experience);
    if (years) next.min_experience_years = Number(years[1]);
  }
  if (skills) next.required_skills = list(skills);
  if (communication) next.communication = communication;
  if (other) next.other_criteria = list(other);
  if (preferred) next.preferred_skills = list(preferred);
  if (offers) next.offers = list(offers);
  if (documents) next.apply_documents = documents;
  if (contact) {
    const foundEmail = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/.exec(contact);
    if (foundEmail) next.contact_email = foundEmail[0].toLowerCase();
  }
  return next;
}

const signedUp: Record<string, { name: string; role: Role }> = {};

export const mockApi: Api = {
  async signup(name, email, password) {
    await delay();
    const key = email.trim().toLowerCase();
    if (!name.trim() || key.includes("..") || !/^[a-z0-9](?:[a-z0-9._%+-]{0,62}[a-z0-9])?@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]{2,}$/.test(key) || password.length < 8) {
      throw new ApiError(422, "Enter your name, a valid email, and a password of at least 8 characters.");
    }
    if (signedUp[key]) throw new ApiError(409, "An account with this email already exists.");
    signedUp[key] = { name: name.trim(), role: "interviewer" };
    audit("signup", "user", key);
    return { token: "mock-token-interviewer", user: { id: `u-${key}`, name: name.trim(), role: "interviewer" } };
  },

  async login(username, password) {
    await delay();
    const users: Record<string, { name: string; role: Role }> = {
      interviewer: { name: "Demo Interviewer", role: "interviewer" },
      admin: { name: "Demo Admin", role: "admin" },
      ...signedUp,
    };
    const u = users[username.trim().toLowerCase()];
    if (!u || password !== "demo-pass") {
      throw new ApiError(401, "The username or password is not correct.");
    }
    audit("login", "user", username);
    return { token: `mock-token-${u.role}`, user: { id: `u-${u.role}`, name: u.name, role: u.role } };
  },

  async joinInterview(joinToken, consent) {
    await delay();
    if (!consent) throw new ApiError(400, "Please accept the notice to join the interview.");
    const it = db.interviews.find((i) => i.join_token === joinToken);
    if (!it) throw new ApiError(404, "This interview link is not valid.");
    return { token: `mock-room-${it.id}`, user: { id: "room-guest", name: it.candidate_name, role: "candidate" }, interview_id: it.id };
  },

  async getDashboard() {
    await delay();
    const c = db.candidates;
    const count = (s: Candidate["status"][]) => c.filter((x) => s.includes(x.status)).length;
    return {
      open_jobs: db.jobs.filter((j) => j.status === "open").length,
      applications_total: c.length,
      awaiting_screening: count(["queued", "processing"]),
      failed_cvs: count(["failed"]),
      interviews_scheduled: db.interviews.filter((i) => i.status === "scheduled").length,
      assessments_ready: db.assessments.length,
      pipeline: [
        { stage: "Received", count: c.length },
        { stage: "Screened", count: c.length - count(["failed", "queued", "processing"]) },
        { stage: "Interviewed", count: count(["interviewed", "assessed"]) },
        { stage: "Assessed", count: count(["assessed"]) },
      ],
      recent: db.audit.slice(0, 8),
    };
  },

  async listJobs() { await delay(); return db.jobs.map((j) => ({ ...j, applicants: db.candidates.filter((c) => c.job_id === j.id).length })); },
  async getJob(id) {
    await delay();
    const j = db.jobs.find((x) => x.id === id);
    if (!j) throw new ApiError(404, "We could not find what you asked for.");
    return { ...j, applicants: db.candidates.filter((c) => c.job_id === id).length };
  },
  async updateWeights(id, weights) {
    await delay();
    const j = db.jobs.find((x) => x.id === id);
    if (!j) throw new ApiError(404, "We could not find what you asked for.");
    if (Object.values(weights).some((v) => v < 0) || Object.values(weights).reduce((a, b) => a + b, 0) <= 0) {
      throw new ApiError(422, "Weights must be positive.");
    }
    j.weights = weights;
    audit("weights_updated", "job", id);
    return j;
  },

  async assistantMessage(jobId, message, template) {
    await delay(500);
    let st = jobId ? db.assistants.get(jobId) : undefined;
    if (!st) {
      const id = `job-${db.jobs.length + 1}`;
      st = { job_id: id, status: "collecting", messages: [], profile: {}, missing_fields: [], approval: null, publish_results: [], apply_email: null };
      db.assistants.set(id, st);
      db.jobs.push({
        id, title: "New role", status: "draft", created_at: now(), applicants: 0,
        profile: { title: "", required_skills: [], preferred_skills: [], min_experience_years: null, qualifications: [], contact_email: "", company: "", remuneration: "", start_date: "", address: "", location_or_mode: "", employment_type: "", experience_level: "", application_deadline: "", about_role: "", responsibilities: [], communication: "", other_criteria: [], offers: [], apply_documents: "" },
        requirements: [], weights: { technical_skills: 0.5, experience: 0.3, qualifications: 0.2 }, post_text: null, apply_email: null, channels: [],
      });
      audit("job_created", "job", id);
    }
    st.messages.push({ role: "user", content: message });
    st.profile = parseInto(st.profile, message);
    st.missing_fields = missingFields(st.profile);
    const job = db.jobs.find((j) => j.id === st!.job_id)!;
    job.title = st.profile.title ?? job.title;
    job.profile = { ...job.profile, ...st.profile } as JobProfile;
    if (st.missing_fields.length) {
      st.status = "collecting";
      st.messages.push({ role: "assistant", content: `Thanks. I still need ${st.missing_fields.map((f) => MANDATORY_LABELS[f]).join(", ")}.` });
    } else {
      st.status = "awaiting_approval";
      job.status = "awaiting_approval";
      st.approval = approvalFor(st.profile);
      if (template?.trim()) st.approval.draft_post = draftPost(st.profile, template.trim());
      st.messages.push({ role: "assistant", content: "I drafted a job post using your company policies. Please review it and choose where to post it." });
    }
    return structuredClone(st);
  },
  async getAssistant(jobId) { await delay(); return structuredClone(ensureAssistant(jobId)); },

  async submitApproval(jobId, d: ApprovalDecision) {
    await delay(500);
    const st = ensureAssistant(jobId);
    const job = db.jobs.find((j) => j.id === jobId)!;
    if (d.action === "reject") {
      st.status = "rejected"; job.status = "rejected"; st.approval = null;
      st.messages.push({ role: "assistant", content: "Understood. The draft was rejected and nothing was published." });
      audit("post_rejected", "job", jobId);
    } else if (d.action === "changes") {
      const revised = approvalFor(st.profile);
      revised.draft_post += `\n\n(Revised after feedback: ${d.feedback ?? "no details"})`;
      st.approval = revised;
      st.messages.push({ role: "assistant", content: "I revised the draft. Please review it again." });
      audit("post_changes_requested", "job", jobId);
    } else {
      const text = d.edited_post ?? st.approval?.draft_post ?? "";
      if (!/equal opportunity/i.test(text)) {
        throw new ApiError(422, "The post must include the equal opportunity statement before it can be published.");
      }
      if (!d.channels.length) throw new ApiError(422, "Choose at least one place to post.");
      st.publish_results = d.channels.map((c) => ({ channel: c, status: "ok", url: `https://blog.example/posts/${jobId}` }));
      st.status = "open"; st.apply_email = "hiring@example.com"; st.approval = null;
      job.status = "open"; job.channels = d.channels; job.apply_email = st.apply_email;
      job.post_text = `${text}\n\nHow to apply: email your CV to ${st.apply_email} with the subject 'Application ${jobId}'.`;
      st.messages.push({ role: "assistant", content: "Published. Candidates can now apply by email." });
      audit("post_approved", "job", jobId);
    }
    return structuredClone(st);
  },

  async listCandidates(jobId) { await delay(); return db.candidates.filter((c) => !jobId || c.job_id === jobId).map((c) => ({ ...c })); },
  async getCandidate(id) {
    await delay();
    const c = db.candidates.find((x) => x.id === id);
    if (!c) throw new ApiError(404, "We could not find what you asked for.");
    const detail = buildDetail(c);
    detail.notes = [...db.notes.values()].filter((n) => n.session_id && db.interviews.some((i) => i.id === n.session_id && i.candidate_id === c.id));
    detail.evaluation = db.assessments.find((a) => a.candidate_id === c.id) ?? null;
    return detail;
  },
  async uploadCvs(jobId, files) {
    await delay(400);
    const results: UploadResult[] = [];
    for (const f of files) {
      const okType = /\.(pdf|docx)$/i.test(f.name);
      if (!okType) { results.push({ filename: f.name, status: "rejected", reason: "Only PDF and DOCX files are accepted." }); continue; }
      if (f.size > MAX_UPLOAD_MB * 1024 * 1024) { results.push({ filename: f.name, status: "rejected", reason: `The file is larger than ${MAX_UPLOAD_MB} MB.` }); continue; }
      const id = `cand-${db.candidates.length + 1}`;
      const cand: Candidate = {
        id, job_id: jobId, name: `Applicant ${String(db.candidates.length + 1).padStart(2, "0")}`,
        status: "queued", aggregate_score: null, source: "upload", received_at: now(), assessment_id: null,
      };
      db.candidates.push(cand);
      setTimeout(() => { cand.status = "processing"; }, 800);
      setTimeout(() => { cand.status = "screened"; cand.aggregate_score = Math.round(45 + Math.random() * 45); }, 3500);
      audit("cv_received", "candidate", id);
      results.push({ filename: f.name, status: "queued", reason: null });
    }
    return results;
  },
  async batchCvs(jobId, files) {
    await delay(400);
    const job = db.jobs.find((j) => j.id === jobId);
    if (!job) throw new ApiError(404, "We could not find what you asked for.");
    const filesOut: CvBatchResult["files"] = [];
    for (const f of files) {
      const okType = /\.(pdf|docx)$/i.test(f.name);
      if (!okType) { filesOut.push({ filename: f.name, status: "rejected", reason: "Only PDF and DOCX files are accepted.", candidate_id: null, aggregate_score: null }); continue; }
      if (f.size > MAX_UPLOAD_MB * 1024 * 1024) { filesOut.push({ filename: f.name, status: "rejected", reason: `The file is larger than ${MAX_UPLOAD_MB} MB.`, candidate_id: null, aggregate_score: null }); continue; }
      const score = stableScore(f.name, f.size);
      const id = `cand-${db.candidates.length + 1}`;
      const cand: Candidate = {
        id, job_id: jobId, name: `Applicant ${String(db.candidates.length + 1).padStart(2, "0")}`,
        status: "screened", aggregate_score: score, source: "upload", received_at: now(), assessment_id: null,
      };
      db.candidates.push(cand);
      job.applicants += 1;
      audit("cv_screened", "candidate", id);
      filesOut.push({ filename: f.name, status: "screened", reason: null, candidate_id: id, aggregate_score: score });
    }
    return { files: filesOut, review: reviewFor(jobId) };
  },
  async getCvReview(jobId) {
    await delay();
    if (!db.jobs.some((j) => j.id === jobId)) throw new ApiError(404, "We could not find what you asked for.");
    return reviewFor(jobId);
  },
  async deleteCandidate(id) {
    await delay();
    db.candidates = db.candidates.filter((c) => c.id !== id);
    db.interviews = db.interviews.filter((i) => i.candidate_id !== id);
    db.assessments = db.assessments.filter((a) => a.candidate_id !== id);
    audit("candidate_deleted", "candidate", id);
  },

  async listInterviews() { await delay(); return db.interviews.map((i) => ({ ...i })); },
  async getInterview(id) {
    await delay();
    const i = db.interviews.find((x) => x.id === id);
    if (!i) throw new ApiError(404, "We could not find what you asked for.");
    return { ...i };
  },
  async createInterview(jobId, candidateId, startsAt) {
    await delay();
    const c = db.candidates.find((x) => x.id === candidateId);
    const j = db.jobs.find((x) => x.id === jobId);
    if (!c || !j) throw new ApiError(404, "We could not find what you asked for.");
    const id = `int-${db.interviews.length + 1}`;
    const it = { id, job_id: jobId, job_title: j.title, candidate_id: candidateId, candidate_name: c.name, status: "scheduled" as const, created_at: now(), scheduled_at: startsAt, started_at: null, ended_at: null, join_token: `join-${id}-demo`, email_sent: true, email_error: null };
    db.interviews.push(it);
    if (c.status === "screened") c.status = "interview_scheduled";
    audit("interview_created", "interview", id);
    return it;
  },
  async rescheduleInterview(id, startsAt) {
    await delay();
    const i = db.interviews.find((x) => x.id === id);
    if (!i) throw new ApiError(404, "We could not find what you asked for.");
    i.scheduled_at = startsAt;
    i.join_token = `join-${id}-moved`;
    i.email_sent = true;
    i.interviewer_email_sent = false;
    i.interviewer_email_error = "This account has no email address. Copy the interviewer link.";
    i.interviewer_room = `/interviews/${id}/room`;
    return { ...i };
  },
  async endInterview(id) {
    await delay();
    const i = db.interviews.find((x) => x.id === id);
    if (!i) return;
    i.status = "completed"; i.ended_at = now(); i.started_at ??= now();
    const c = db.candidates.find((x) => x.id === i.candidate_id);
    if (c && c.status !== "assessed") c.status = "interviewed";
    audit("interview_ended", "interview", id);
  },
  async getTranscript(id) {
    await delay();
    const i = db.interviews.find((x) => x.id === id);
    return { interview_id: id, candidate_id: i?.candidate_id ?? "", job_id: i?.job_id ?? "", started_at: i?.started_at ?? null, ended_at: i?.ended_at ?? null, turns: TRANSCRIPT };
  },
  async getBrief(id) {
    await delay();
    const i = db.interviews.find((x) => x.id === id);
    const c = i && db.candidates.find((x) => x.id === i.candidate_id);
    if (!i || !c) throw new ApiError(404, "We could not find what you asked for.");
    const detail = buildDetail(c);
    return { name: c.name, role: i.job_title, aggregate_score: c.aggregate_score, dimension_scores: detail.dimension_scores, matches: detail.matches, gaps: detail.matches.filter((m) => m.status !== "met") };
  },
  async copilotQuota(id) {
    await delay();
    const used = db.copilotUsed.get(id) ?? 0;
    return { remaining: Math.max(0, 15 - used), limit: 15 };
  },
  async askCopilot(id, _message): Promise<CopilotReply> {
    await delay();
    const used = (db.copilotUsed.get(id) ?? 0) + 1;
    if (used > 15) throw new ApiError(429, "No co-pilot questions left for this interview (0 of 15).");
    db.copilotUsed.set(id, used);
    return {
      remaining: 15 - used, limit: 15,
      questions: [
        { question: "How have you designed a REST API?", requirement_id: "R1", reference_answer: "A concrete service and a versioning choice." },
        { question: "Where have you used Docker?", requirement_id: "R2", reference_answer: "An image they built and how it was deployed." },
      ],
    };
  },
  async getNotes(id) {
    await delay();
    return db.notes.get(id) ?? { session_id: id, author_name: "", body: "", markers: [], updated_at: null };
  },
  async saveNotes(id, body, mark) {
    await delay();
    if (body.length > 5000) throw new ApiError(422, "Some of the information looks invalid. Please check it and try again.");
    const i = db.interviews.find((x) => x.id === id);
    const current = db.notes.get(id) ?? { session_id: id, author_name: "Demo Interviewer", body: "", markers: [], updated_at: null };
    const markers = mark ? [...current.markers, { offset_ms: 1000, at: now() }] : current.markers;
    const note = { ...current, session_id: id, author_name: getSession()?.user.name || "Demo Interviewer", body, markers, updated_at: now() };
    db.notes.set(id, note);
    if (i) audit("interview_note_saved", "interview", id);
    return note;
  },

  async listAssessments() {
    await delay();
    return db.assessments.map(({ id, candidate_id, candidate_name, job_id, job_title, overall_position, combined, status, created_at }) => ({ id, candidate_id, candidate_name, job_id, job_title, overall_position, combined, status, created_at }));
  },
  async getAssessment(id) {
    await delay();
    const a = db.assessments.find((x) => x.id === id);
    if (!a) throw new ApiError(404, "We could not find what you asked for.");
    return a;
  },
  async generateAssessment(interviewId) {
    await delay(700);
    const runs = (db.evalRuns.get(interviewId) ?? 0) + 1;
    if (runs > 2) throw new ApiError(429, "This interview has already been evaluated 2 times.");
    db.evalRuns.set(interviewId, runs);
    const i = db.interviews.find((x) => x.id === interviewId);
    const c = i && db.candidates.find((x) => x.id === i.candidate_id);
    if (!i || !c) throw new ApiError(404, "We could not find what you asked for.");
    const existing = db.assessments.find((a) => a.candidate_id === c.id);
    if (existing) return existing;
    const score = c.aggregate_score ?? 60;
    const asm = buildAssessment(`asm-${db.assessments.length + 1}`, c, i.id, score >= 75 ? "strong" : score >= 60 ? "promising" : "uncertain", Math.max(40, score - 6));
    db.assessments.push(asm);
    c.status = "assessed"; c.assessment_id = asm.id;
    audit("assessment_generated", "assessment", asm.id);
    return asm;
  },
  async getEvidence(ref) {
    await delay(120);
    const [kind, a, b] = ref.split(":");
    if (kind === "tr") return { ref, text: TRANSCRIPT.find((t) => t.turn_id === b)?.text ?? "Transcript segment." };
    if (kind === "req") return { ref, text: REQUIREMENTS.find((r) => r.id === a)?.text ?? "Job requirement." };
    if (kind === "pol") return { ref, text: POLICY_EVIDENCE.find((p) => p.ref === ref)?.text ?? "Policy section." };
    const keys = Object.keys(EVIDENCE_TEXT);
    const idx = Number((b ?? "c001").slice(1)) - 1;
    return { ref, text: EVIDENCE_TEXT[keys[idx] ?? "R1"] ?? "CV section." };
  },

  async getJobTemplate() { await delay(); return { text: storedTemplate ?? DEFAULT_TEMPLATE, stored: storedTemplate !== null }; },
  async saveJobTemplate(text) {
    await delay();
    storedTemplate = text.trim();
    const existing = db.policies.find((p) => p.title === "Job description template" && p.status === "active");
    if (existing) existing.chunks = 1;
    else db.policies.unshift({ id: "pol-template", title: "Job description template", version: "1.0", effective_date: now(), status: "active", chunks: 1 });
    audit("policy_uploaded", "policy", "pol-template");
    return { text: storedTemplate, stored: true };
  },
  async listPolicies() { await delay(); return db.policies.map((p) => ({ ...p })); },
  async uploadPolicy(file) {
    await delay(500);
    if (!/\.(pdf|docx|txt|md)$/i.test(file.name)) throw new ApiError(422, "Only PDF, DOCX, TXT or MD files are accepted.");
    const p: Policy = { id: `pol-${db.policies.length + 1}`, title: file.name.replace(/\.[^.]+$/, ""), version: "1.0", effective_date: now(), status: "active", chunks: 8 };
    db.policies.push(p);
    audit("policy_uploaded", "policy", p.id);
    return p;
  },
  async deletePolicy(id) { await delay(); db.policies = db.policies.filter((p) => p.id !== id); audit("policy_deleted", "policy", id); },

  async getAudit() { await delay(); return db.audit.map((e) => ({ ...e })); },
  async sendClientLogs() { /* demo mode: nothing to ship */ },
};
