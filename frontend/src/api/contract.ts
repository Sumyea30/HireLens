import type {
  ApprovalDecision, Assessment, AssessmentSummary, AssistantReply, AuditEvent, Candidate,
  CandidateDetail, CopilotReply, CvBatchResult, CvReview, DashboardSummary, EvidenceItem, Interview, InterviewBrief,
  InterviewNote, Job, Policy, Session, TranscriptDocument, UploadResult,
} from "../lib/types";
import type { LogEntry } from "../lib/logger";

/** The contract the FastAPI backend must implement. Real and mock clients both satisfy it. */
export interface Api {
  login(username: string, password: string): Promise<Session>;
  signup(name: string, email: string, password: string): Promise<Session>;
  joinInterview(joinToken: string, consent: boolean): Promise<Session>;
  getDashboard(): Promise<DashboardSummary>;

  listJobs(): Promise<Job[]>;
  getJob(id: string): Promise<Job>;
  updateWeights(id: string, weights: Record<string, number>): Promise<Job>;
  assistantMessage(jobId: string | null, message: string, template?: string): Promise<AssistantReply>;
  getAssistant(jobId: string): Promise<AssistantReply>;
  submitApproval(jobId: string, decision: ApprovalDecision): Promise<AssistantReply>;

  listCandidates(jobId?: string): Promise<Candidate[]>;
  getCandidate(id: string): Promise<CandidateDetail>;
  uploadCvs(jobId: string, files: File[]): Promise<UploadResult[]>;
  batchCvs(jobId: string, files: File[]): Promise<CvBatchResult>;
  getCvReview(jobId: string): Promise<CvReview>;
  deleteCandidate(id: string): Promise<void>;

  listInterviews(): Promise<Interview[]>;
  getInterview(id: string): Promise<Interview>;
  createInterview(jobId: string, candidateId: string, startsAt: string): Promise<Interview>;
  rescheduleInterview(id: string, startsAt: string): Promise<Interview>;
  endInterview(id: string): Promise<void>;
  getTranscript(id: string): Promise<TranscriptDocument>;
  getBrief(id: string): Promise<InterviewBrief>;
  copilotQuota(id: string): Promise<{ remaining: number; limit: number }>;
  askCopilot(id: string, message: string): Promise<CopilotReply>;
  getNotes(id: string): Promise<InterviewNote>;
  saveNotes(id: string, body: string, mark?: boolean): Promise<InterviewNote>;

  listAssessments(): Promise<AssessmentSummary[]>;
  getAssessment(id: string): Promise<Assessment>;
  generateAssessment(interviewId: string): Promise<Assessment>;
  getEvidence(ref: string): Promise<EvidenceItem>;

  getJobTemplate(): Promise<{ text: string; stored: boolean }>;
  saveJobTemplate(text: string): Promise<{ text: string; stored: boolean }>;
  listPolicies(): Promise<Policy[]>;
  uploadPolicy(file: File): Promise<Policy>;
  deletePolicy(id: string): Promise<void>;

  getAudit(): Promise<AuditEvent[]>;
  sendClientLogs(entries: LogEntry[]): Promise<void>;
}
