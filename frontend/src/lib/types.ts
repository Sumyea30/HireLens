export type Role = "interviewer" | "admin" | "candidate";
export interface User { id: string; name: string; role: Role }
export interface Session { token: string; user: User; interview_id?: string }

export interface JobProfile {
  title: string;
  required_skills: string[];
  preferred_skills: string[];
  min_experience_years: number | null;
  qualifications: string[];
  contact_email: string;
  company: string;
  remuneration: string;
  start_date: string;
  address: string;
  location_or_mode: string;
  employment_type: string;
  experience_level: string;
  application_deadline: string;
  about_role: string;
  responsibilities: string[];
  communication: string;
  other_criteria: string[];
  offers: string[];
  apply_documents: string;
}
export interface Requirement { id: string; text: string; kind: "required" | "preferred"; category: string }
export type JobStatus = "draft" | "awaiting_approval" | "open" | "closed" | "rejected" | "failed";
export interface Job {
  id: string;
  title: string;
  status: JobStatus;
  created_at: string;
  applicants: number;
  profile: JobProfile;
  requirements: Requirement[];
  weights: Record<string, number>;
  post_text: string | null;
  apply_email: string | null;
  channels: string[];
}

export type CandidateStatus =
  | "queued" | "processing" | "screened" | "failed" | "incomplete"
  | "interview_scheduled" | "interviewed" | "assessed";
export type FaceCheckResult = { match: boolean; score: number; reason: string };
export interface Candidate {
  id: string;
  job_id: string;
  name: string;
  status: CandidateStatus;
  aggregate_score: number | null;
  source: "email" | "upload";
  received_at: string;
  assessment_id: string | null;
  has_photo?: boolean;
  photo_url?: string | null;
  real_name?: string | null;
}
export interface EvidenceItem { ref: string; text: string }
export type MatchStatus = "met" | "partial" | "unmet";
export interface RequirementMatch {
  requirement_id: string;
  requirement_text: string;
  status: MatchStatus;
  justification: string;
  evidence: EvidenceItem[];
}
export interface DimensionScore {
  dimension: string;
  score: number;
  weight: number;
  justification: string;
  evidence_refs: string[];
}
export interface CandidateDetail extends Candidate {
  email: string | null;
  phone: string | null;
  failure_reason: string | null;
  parsed: {
    skills: string[];
    education: string[];
    work_history: string[];
    certifications: string[];
    projects: string[];
    total_experience_years: number | null;
  };
  dimension_scores: DimensionScore[];
  matches: RequirementMatch[];
  notes: InterviewNote[];
  evaluation: Assessment | null;
}

export type InterviewStatus = "scheduled" | "live" | "completed" | "interrupted";
export interface Interview {
  id: string;
  job_id: string;
  job_title: string;
  candidate_id: string;
  candidate_name: string;
  status: InterviewStatus;
  created_at: string;
  scheduled_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  join_token: string;
  email_sent?: boolean;
  email_error?: string | null;
  interviewer_email_sent?: boolean;
  interviewer_email_error?: string | null;
  interviewer_room?: string;
}
export interface InterviewNote {
  session_id?: string;
  author_name: string;
  body: string;
  markers: { offset_ms: number; at: string }[];
  updated_at: string | null;
}
export interface InterviewBrief {
  name: string;
  role: string;
  aggregate_score: number | null;
  dimension_scores: DimensionScore[];
  matches: RequirementMatch[];
  gaps: RequirementMatch[];
}
export interface CopilotQuestion {
  question: string;
  requirement_id: string;
  reference_answer: string;
}
export interface CopilotReply {
  questions: CopilotQuestion[];
  remaining: number;
  limit: number;
}
export interface TranscriptTurn {
  turn_id: string;
  speaker: "interviewer" | "candidate";
  start_ms: number;
  end_ms: number;
  text: string;
  confidence?: number | null;
  original_text?: string | null;
  edited?: boolean;
  /** Non-Latin script detected; excluded from eval until edited. */
  language_mismatch?: boolean;
}
export interface TranscriptDocument {
  interview_id: string;
  candidate_id: string;
  job_id: string;
  started_at: string | null;
  ended_at: string | null;
  turns: TranscriptTurn[];
}
export interface CoverageItem { requirement_id: string; text: string; state: "covered" | "not_yet" }

export type Position = "strong" | "promising" | "uncertain" | "weak";
export interface Finding { statement: string; evidence_refs: string[] }
export interface QAEval {
  pair_id: string;
  question: string;
  answer: string;
  relevance: number;
  accuracy: number;
  completeness: number;
  quality: number;
  justification: string;
  evidence_refs: string[];
}
export interface AssessmentSummary {
  id: string;
  candidate_id: string;
  candidate_name: string;
  job_id: string;
  job_title: string;
  overall_position: Position;
  combined: number;
  status: "completed" | "partial";
  created_at: string;
}
export interface Assessment extends AssessmentSummary {
  session_id: string;
  has_interview: boolean;
  component_scores: { cv_score: number; interview_score: number | null; combined: number };
  strengths: Finding[];
  weaknesses: Finding[];
  evidence_gaps: Finding[];
  follow_up_areas: Finding[];
  requirements: Requirement[];
  requirement_matches: RequirementMatch[];
  qa_evaluations: QAEval[];
  uncovered_requirement_ids: string[];
}

export interface Policy {
  id: string;
  title: string;
  version: string;
  effective_date: string;
  status: "active" | "superseded";
  chunks: number;
}
export interface AuditEvent {
  id: string;
  ts: string;
  actor_role: Role | "system";
  action: string;
  entity: string;
  entity_id: string;
  outcome: "ok" | "denied" | "error";
}

export interface ChatMessage { role: "user" | "assistant"; content: string }
export interface ComplianceFinding { severity: "blocker" | "warning" | "info"; message: string; policy_refs: string[] }
export interface ApprovalRequest {
  draft_post: string;
  proposed_channels: string[];
  policy_evidence: EvidenceItem[];
  compliance_findings: ComplianceFinding[];
  mandatory_failures: string[];
  unresolved_items: string[];
}
export interface ApprovalDecision {
  action: "approve" | "changes" | "reject";
  channels: string[];
  edited_post: string | null;
  feedback: string | null;
}
export interface AssistantReply {
  job_id: string;
  status: JobStatus | "collecting" | "failed";
  messages: ChatMessage[];
  profile: Partial<JobProfile>;
  missing_fields: string[];
  approval: ApprovalRequest | null;
  publish_results: { channel: string; status: string; url: string | null }[];
  apply_email: string | null;
}

export interface DashboardSummary {
  open_jobs: number;
  applications_total: number;
  awaiting_screening: number;
  failed_cvs: number;
  interviews_scheduled: number;
  assessments_ready: number;
  pipeline: { stage: string; count: number }[];
  recent: AuditEvent[];
}
export interface UploadResult { filename: string; status: "queued" | "rejected"; reason: string | null }

export interface ReviewMatch {
  requirement_id: string;
  requirement_text: string;
  status: MatchStatus;
  justification: string;
}
export interface ReviewDimension { dimension: string; score: number; justification: string }
export interface ReviewCandidate {
  id: string;
  name: string;
  rank: number;
  aggregate_score: number;
  why: string;
  met: number;
  partial: number;
  unmet: number;
  matches: ReviewMatch[];
  dimensions: ReviewDimension[];
}
export interface UnscoredCv { id: string; name: string; status: string; reason: string }
export interface CvReview {
  job_id: string;
  job_title: string;
  why_these: string;
  candidates: ReviewCandidate[];
  unscored: UnscoredCv[];
}
export interface CvBatchFile {
  filename: string;
  status: "queued" | "rejected" | "screened" | "failed";
  reason: string | null;
  candidate_id: string | null;
  aggregate_score: number | null;
}
export interface CvBatchResult { files: CvBatchFile[]; review: CvReview }

export type FunnelLabel = "bottleneck" | "non_discriminating" | "balanced";
export interface FunnelRequirement {
  requirement_id: string;
  text: string;
  kind: "required" | "preferred";
  mandatory: boolean;
  met: number;
  partial: number;
  unmet: number;
  pass_rate: number;
  label: FunnelLabel;
}
export interface FunnelCandidate {
  id: string;
  name: string;
  aggregate_score: number;
  coverage_estimate: number;
  in_shortlist: boolean;
}
export interface FunnelWhatIf {
  requirement_id: string;
  text: string;
  shortlist_size: number;
  entering: { id: string; name: string; coverage_estimate: number }[];
}
export interface FunnelNearMiss {
  candidate_id: string;
  name: string;
  requirement_id: string;
  requirement_text: string;
  evidence_refs: string[];
  coverage_estimate: number;
  aggregate_score: number;
}
export interface FunnelReport {
  job_id: string;
  job_title: string;
  candidate_count: number;
  low_sample: boolean;
  shortlist_n: number;
  shortlist: { id: string; name: string; aggregate_score: number; coverage_estimate: number }[];
  candidates: FunnelCandidate[];
  requirements: FunnelRequirement[];
  what_if: FunnelWhatIf[];
  near_miss: FunnelNearMiss[];
  thresholds: { bottleneck_max: number; flat_min: number; shortlist_n: number };
  coverage_note: string;
}
export interface FunnelSuggestResult {
  summary: string;
  feedback: string;
  assistant: AssistantReply | null;
  fed_to_assistant: boolean;
}

// WebSocket protocol for the interview room (see README, "Realtime protocol")
export type SignalKind = "offer" | "answer" | "ice";
export type SttStatusState = "listening" | "transcribing" | "unavailable";
export type RoomIn =
  | { type: "signal"; kind: SignalKind; data: unknown }
  | { type: "transcript"; turn: TranscriptTurn }
  | { type: "stt_status"; state?: SttStatusState; pending?: number; message?: string }
  | { type: "speech_activity"; active: boolean; speaker: "interviewer" | "candidate" | string }
  | { type: "coverage"; items: CoverageItem[] }
  | { type: "peer"; state: "joined" | "left" | "ready" }
  | { type: "ended" };
export type RoomOut =
  | { type: "auth"; token: string }
  | { type: "ready" }
  | { type: "signal"; kind: SignalKind; data: unknown }
  | { type: "speech"; text: string };
