// Synthetic demo data only (N13). No real people, CVs or transcripts.
import type {
  Assessment, AuditEvent, Candidate, CandidateDetail, CandidateStatus, Finding, Interview, Job,
  MatchStatus, Policy, Requirement, RequirementMatch, TranscriptTurn,
} from "../lib/types";

const iso = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60_000).toISOString();

export const REQUIREMENTS: Requirement[] = [
  { id: "R1", text: "Python web development (Django or FastAPI)", kind: "required", category: "skill" },
  { id: "R2", text: "REST API design", kind: "required", category: "skill" },
  { id: "R3", text: "Docker and containerised deployment", kind: "required", category: "skill" },
  { id: "R4", text: "SQL databases (PostgreSQL)", kind: "preferred", category: "skill" },
  { id: "R5", text: "At least 3 years of relevant experience", kind: "required", category: "experience" },
  { id: "R6", text: "BSc in Computer Science or a related field", kind: "required", category: "qualification" },
];

export const EVIDENCE_TEXT: Record<string, string> = {
  R1: "Built and maintained Django services for an internal tools platform, including background jobs.",
  R2: "Designed versioned REST endpoints with validation, pagination and OpenAPI documentation.",
  R3: "Containerised services with Docker and Docker Compose for local and staging environments.",
  R4: "Modelled relational schemas and wrote PostgreSQL queries for reporting features.",
  R5: "Backend developer for several years across two employers (see work history).",
  R6: "BSc in Computer Science and Engineering, final project on backend systems.",
};

export const POLICY_EVIDENCE = [
  { ref: "pol:pol-1:c001", text: "Job posts must use approved job titles and list required and preferred skills separately." },
  { ref: "pol:pol-2:c001", text: "Every post must include the company's equal opportunity statement." },
];

const base = (id: string, name: string, status: CandidateStatus, score: number | null, minutes: number,
  source: "email" | "upload" = "email", assessment: string | null = null): Candidate => ({
  id, job_id: "job-1", name, status, aggregate_score: score, source, received_at: iso(minutes), assessment_id: assessment,
});

export function seedCandidates(): Candidate[] {
  return [
    base("cand-1", "Nadia Karim", "assessed", 88, 3000, "email", "asm-1"),
    base("cand-2", "Imran Hossain", "assessed", 81, 2900, "email", "asm-2"),
    base("cand-3", "Farhana Sultana", "interview_scheduled", 74, 2800, "upload"),
    base("cand-4", "Tanvir Ahmed", "screened", 66, 2700, "email"),
    base("cand-5", "Mehnaz Rahim", "screened", 52, 2600, "upload"),
    base("cand-6", "Applicant 06", "failed", null, 2500, "email"),
  ];
}

const statusFor = (i: number, score: number): MatchStatus => {
  const table: MatchStatus[][] = [
    ["met", "met", "met", "met", "partial", "met"],
    ["met", "met", "partial", "partial", "met", "met"],
    ["met", "partial", "unmet", "partial", "partial", "met"],
    ["partial", "unmet", "unmet", "unmet", "unmet", "partial"],
  ];
  const row = score >= 80 ? 0 : score >= 70 ? 1 : score >= 60 ? 2 : 3;
  return table[row][i];
};

export function buildMatches(candidateId: string, score: number, reqs: Requirement[] = REQUIREMENTS): RequirementMatch[] {
  return reqs.map((r, i) => {
    const status = statusFor(i % 6, score);
    return {
      requirement_id: r.id,
      requirement_text: r.text,
      status,
      justification:
        status === "met" ? "The CV shows direct, recent experience for this requirement."
        : status === "partial" ? "Related experience is shown, but it is limited or not recent."
        : "No supporting evidence was found in the CV.",
      evidence: status === "unmet" ? [] : [{ ref: `cv:${candidateId}:c00${(i % 3) + 1}`, text: EVIDENCE_TEXT[r.id] ?? "Relevant experience described in the CV." }],
    };
  });
}

export function buildDetail(c: Candidate): CandidateDetail {
  const s = c.aggregate_score ?? 0;
  const clip = (n: number) => Math.max(0, Math.min(100, n));
  const failed = c.status === "failed" || c.aggregate_score === null;
  return {
    ...c,
    email: failed ? null : `${c.name.toLowerCase().replace(/[^a-z]+/g, ".")}@example.com`,
    phone: failed ? null : "+000 0000 000000",
    failure_reason: c.status === "failed" ? "The file could not be read. It may be scanned, protected or damaged." : null,
    parsed: failed
      ? { skills: [], education: [], work_history: [], certifications: [], projects: [], total_experience_years: null }
      : {
          skills: ["Python", "Django", "FastAPI", "PostgreSQL", "Docker", "REST"].slice(0, s > 70 ? 6 : 4),
          education: ["BSc in Computer Science and Engineering"],
          work_history: ["Backend Developer, Company A (2 years)", "Software Engineer, Company B (2 years)"],
          certifications: s > 75 ? ["Cloud practitioner certificate"] : [],
          projects: ["Inventory API with background workers", "Reporting dashboard backend"],
          total_experience_years: s > 70 ? 4 : 2,
        },
    dimension_scores: failed ? [] : [
      { dimension: "technical_skills", score: clip(s + 3), weight: 0.5, justification: "Stack closely matches the required skills.", evidence_refs: [`cv:${c.id}:c001`] },
      { dimension: "experience", score: clip(s - 4), weight: 0.3, justification: "Relevant years of backend work, partly in different domains.", evidence_refs: [`cv:${c.id}:c002`] },
      { dimension: "qualifications", score: clip(s + 6), weight: 0.2, justification: "Degree matches the required field.", evidence_refs: [`cv:${c.id}:c003`] },
    ],
    matches: failed ? [] : buildMatches(c.id, s),
    notes: [],
    evaluation: null,
  };
}

export function seedJobs(): Job[] {
  return [
    {
      id: "job-1", title: "Backend Developer", status: "open", created_at: iso(4000), applicants: 6,
      profile: {
        title: "Backend Developer", required_skills: ["Python web development", "REST API design", "Docker"],
        preferred_skills: ["PostgreSQL"], min_experience_years: 3, qualifications: ["BSc in Computer Science"],
        contact_email: "hiring@example.com", company: "HireLens", remuneration: "80000 BDT per month", start_date: "", address: "", location_or_mode: "Dhaka, Bangladesh", employment_type: "Full-time", experience_level: "Mid-level", application_deadline: "1 November 2026", about_role: "Build and maintain hiring tools.", responsibilities: ["Build and maintain APIs", "Review code"], communication: "Clear written English", other_criteria: [], offers: ["Learning budget"], apply_documents: "a CV",
      },
      requirements: REQUIREMENTS, weights: { technical_skills: 0.5, experience: 0.3, qualifications: 0.2 },
      post_text: "Backend Developer\n\nWe are hiring a Backend Developer to build and maintain APIs.\nWe are an equal opportunity employer.\n\nHow to apply: email your CV to hiring@example.com with the subject 'Application job-1'.",
      apply_email: "hiring@example.com", channels: ["blogger"],
    },
    {
      id: "job-2", title: "Data Analyst", status: "awaiting_approval", created_at: iso(90), applicants: 0,
      profile: { title: "Data Analyst", required_skills: ["SQL", "Data visualisation"], preferred_skills: [], min_experience_years: 2, qualifications: ["Bachelor's degree"], contact_email: "hiring@example.com", company: "HireLens", remuneration: "60000 BDT per month", start_date: "", address: "", location_or_mode: "Remote", employment_type: "Full-time", experience_level: "Mid-level", application_deadline: "15 November 2026", about_role: "Analyse hiring data.", responsibilities: ["Prepare reports"], communication: "", other_criteria: [], offers: [], apply_documents: "a CV" },
      requirements: [], weights: { technical_skills: 0.5, experience: 0.3, qualifications: 0.2 },
      post_text: null, apply_email: null, channels: [],
    },
  ];
}

export function seedInterviews(): Interview[] {
  const mk = (id: string, c: string, name: string, status: Interview["status"], minutes: number): Interview => ({
    id, job_id: "job-1", job_title: "Backend Developer", candidate_id: c, candidate_name: name, status,
    created_at: iso(minutes + 60), scheduled_at: iso(status === "scheduled" ? -2 : minutes),
    started_at: status === "scheduled" ? null : iso(minutes),
    ended_at: status === "completed" ? iso(minutes - 30) : null, join_token: `join-${id}-demo`,
  });
  return [mk("int-1", "cand-1", "Nadia Karim", "completed", 2000), mk("int-2", "cand-2", "Imran Hossain", "completed", 1900), mk("int-3", "cand-3", "Farhana Sultana", "scheduled", 60)];
}

export const TRANSCRIPT: TranscriptTurn[] = [
  { turn_id: "t1", speaker: "interviewer", start_ms: 1000, end_ms: 6000, text: "Can you describe a REST API you designed and how you handled versioning?" },
  { turn_id: "t2", speaker: "candidate", start_ms: 7000, end_ms: 24000, text: "I built a Django REST service for an inventory tool. We versioned through the URL path and kept old versions for one release cycle." },
  { turn_id: "t3", speaker: "interviewer", start_ms: 25000, end_ms: 29000, text: "How did you deploy it?" },
  { turn_id: "t4", speaker: "candidate", start_ms: 30000, end_ms: 42000, text: "With Docker Compose on a staging server, and a simple pipeline that built the image on every merge." },
  { turn_id: "t5", speaker: "interviewer", start_ms: 43000, end_ms: 48000, text: "What did you use for the database, and how did you test queries?" },
  { turn_id: "t6", speaker: "candidate", start_ms: 49000, end_ms: 62000, text: "PostgreSQL. I used a test database in the CI run and checked the slow queries with EXPLAIN." },
];

const f = (statement: string, ...refs: string[]): Finding => ({ statement, evidence_refs: refs });

export function buildAssessment(id: string, c: Candidate, sessionId: string, position: Assessment["overall_position"], interviewScore: number): Assessment {
  const cv = c.aggregate_score ?? 0;
  const combined = Math.round(((cv + interviewScore) / 2) * 100) / 100;
  return {
    id, candidate_id: c.id, candidate_name: c.name, job_id: "job-1", job_title: "Backend Developer",
    overall_position: position, combined, status: "completed", created_at: iso(1800), session_id: sessionId, has_interview: true,
    component_scores: { cv_score: cv, interview_score: interviewScore, combined },
    strengths: [
      f("Clear, specific description of API versioning in practice.", `tr:${sessionId}:t2`),
      f("Container-based deployment confirmed in the interview and the CV.", `tr:${sessionId}:t4`, `cv:${c.id}:c001`),
    ],
    weaknesses: [f("Testing approach was described briefly and without detail.", `tr:${sessionId}:t6`)],
    evidence_gaps: [f("Experience with message queues was not covered in the interview.", "req:R4")],
    follow_up_areas: [f("Ask about scaling decisions and failure handling in the inventory service.", `tr:${sessionId}:t2`)],
    requirements: REQUIREMENTS, requirement_matches: buildMatches(c.id, cv),
    qa_evaluations: [
      { pair_id: "p001", question: TRANSCRIPT[0].text, answer: TRANSCRIPT[1].text, relevance: 9, accuracy: 8, completeness: 8, quality: 8, justification: "Specific and on topic, with a concrete versioning approach.", evidence_refs: [`tr:${sessionId}:t2`] },
      { pair_id: "p002", question: TRANSCRIPT[2].text, answer: TRANSCRIPT[3].text, relevance: 9, accuracy: 8, completeness: 7, quality: 8, justification: "Names the tooling and the pipeline step.", evidence_refs: [`tr:${sessionId}:t4`] },
      { pair_id: "p003", question: TRANSCRIPT[4].text, answer: TRANSCRIPT[5].text, relevance: 8, accuracy: 8, completeness: 6, quality: 7, justification: "Correct tools, limited detail on testing strategy.", evidence_refs: [`tr:${sessionId}:t6`] },
    ],
    uncovered_requirement_ids: ["R5"],
  };
}

export function seedPolicies(): Policy[] {
  return [
    { id: "pol-1", title: "Job posting standards (demo)", version: "2.0", effective_date: iso(20000), status: "active", chunks: 9 },
    { id: "pol-2", title: "Equal opportunity statement (demo)", version: "1.1", effective_date: iso(40000), status: "active", chunks: 3 },
    { id: "pol-3", title: "Compensation bands (demo)", version: "1.0", effective_date: iso(60000), status: "superseded", chunks: 6 },
  ];
}

export function seedAudit(): AuditEvent[] {
  const rows: [AuditEvent["actor_role"], string, string, string, AuditEvent["outcome"]][] = [
    ["interviewer", "login", "user", "u-1", "ok"], ["interviewer", "job_created", "job", "job-1", "ok"],
    ["interviewer", "post_approved", "job", "job-1", "ok"], ["system", "cv_received", "candidate", "cand-1", "ok"],
    ["system", "cv_screened", "candidate", "cand-1", "ok"], ["system", "cv_failed", "candidate", "cand-6", "error"],
    ["interviewer", "interview_created", "interview", "int-1", "ok"], ["interviewer", "interview_ended", "interview", "int-1", "ok"],
    ["system", "assessment_generated", "assessment", "asm-1", "ok"], ["admin", "policy_uploaded", "policy", "pol-1", "ok"],
    ["interviewer", "candidate_deleted", "candidate", "cand-x", "denied"],
  ];
  return rows.map(([actor_role, action, entity, entity_id, outcome], i) => ({
    id: `ev-${i + 1}`, ts: iso(3000 - i * 240), actor_role, action, entity, entity_id, outcome,
  })).reverse();
}
