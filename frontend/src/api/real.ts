import type { Api } from "./contract";
import type { CvBatchResult, UploadResult } from "../lib/types";
import { ApiError, request } from "./client";

const enc = encodeURIComponent;

export const realApi: Api = {
  login: (username, password) => request("POST", "/auth/login", { username, password }),
  signup: (name, email, password) => request("POST", "/auth/signup", { name, email, password }),
  joinInterview: (joinToken, consent) => request("POST", `/join/${enc(joinToken)}`, { consent }),
  getDashboard: () => request("GET", "/dashboard/summary"),

  listJobs: () => request("GET", "/jobs"),
  getJob: (id) => request("GET", `/jobs/${enc(id)}`),
  updateWeights: (id, weights) => request("PUT", `/jobs/${enc(id)}/weights`, { weights }),
  getFunnel: (jobId) => request("GET", `/jobs/${enc(jobId)}/funnel`),
  suggestFunnelRevision: (jobId) => request("POST", `/jobs/${enc(jobId)}/funnel/suggest-revision`),
  assistantMessage: (jobId, message, template) => request("POST", "/jobs/assistant", { job_id: jobId, message, template: template ?? null }),
  getAssistant: (jobId) => request("GET", `/jobs/${enc(jobId)}/assistant`),
  submitApproval: (jobId, decision) => request("POST", `/jobs/${enc(jobId)}/approval`, decision),

  checkMail: () => request("POST", "/mail/check"),
  listCandidates: (jobId) => request("GET", jobId ? `/candidates?job_id=${enc(jobId)}` : "/candidates"),
  getCandidate: (id) => request("GET", `/candidates/${enc(id)}`),
  // One request per file: a request over the proxy's size limit then only rejects that one file,
  // and the other files in the selection still go through.
  uploadCvs: async (jobId, files) => {
    const results: UploadResult[] = [];
    for (const f of files) {
      const form = new FormData();
      form.append("files", f);
      try {
        results.push(...(await request<UploadResult[]>("POST", `/jobs/${enc(jobId)}/cvs`, undefined, form)));
      } catch (e) {
        results.push({ filename: f.name, status: "rejected", reason: e instanceof ApiError ? e.userMessage : "The upload failed. Please try again." });
      }
    }
    return results;
  },
  batchCvs: async (jobId, files) => {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    return request<CvBatchResult>("POST", `/jobs/${enc(jobId)}/cv-batch`, undefined, form);
  },
  getCvReview: (jobId) => request("GET", `/jobs/${enc(jobId)}/cv-review`),
  deleteCandidate: (id) => request("DELETE", `/admin/candidates/${enc(id)}`),

  listInterviews: () => request("GET", "/interviews"),
  getInterview: (id) => request("GET", `/interviews/${enc(id)}`),
  createInterview: (jobId, candidateId, startsAt) => request("POST", "/interviews", { job_id: jobId, candidate_id: candidateId, starts_at: startsAt }),
  rescheduleInterview: (id, startsAt) => request("POST", `/interviews/${enc(id)}/reschedule`, { starts_at: startsAt }),
  endInterview: (id) => request("POST", `/interviews/${enc(id)}/end`),
  getTranscript: (id) => request("GET", `/interviews/${enc(id)}/transcript`),
  editTranscriptTurn: (id, turnId, text) => request("PATCH", `/interviews/${enc(id)}/transcript/turns/${enc(turnId)}`, { text }),
  getBrief: (id) => request("GET", `/interviews/${enc(id)}/brief`),
  faceCheck: async (id, image) => {
    const form = new FormData();
    form.append("image", image, "frame.jpg");
    return request("POST", `/interviews/${enc(id)}/face-check`, undefined, form);
  },
  copilotQuota: (id) => request("GET", `/interviews/${enc(id)}/copilot`),
  askCopilot: (id, message) => request("POST", `/interviews/${enc(id)}/copilot`, { message }),
  getNotes: (id) => request("GET", `/interviews/${enc(id)}/notes`),
  saveNotes: (id, body, mark) => request("PUT", `/interviews/${enc(id)}/notes`, { body, mark: Boolean(mark) }),

  listAssessments: () => request("GET", "/assessments"),
  getAssessment: (id) => request("GET", `/assessments/${enc(id)}`),
  generateAssessment: (interviewId) => request("POST", `/interviews/${enc(interviewId)}/assessment`),
  getEvidence: (ref) => request("GET", `/evidence?ref=${enc(ref)}`),

  getJobTemplate: () => request("GET", "/policies/template"),
  saveJobTemplate: (text) => request("PUT", "/policies/template", { text }),
  listPolicies: () => request("GET", "/policies"),
  uploadPolicy: (file) => {
    const form = new FormData();
    form.append("file", file);
    return request("POST", "/policies", undefined, form);
  },
  deletePolicy: (id) => request("DELETE", `/policies/${enc(id)}`),

  getAudit: () => request("GET", "/audit"),
  sendClientLogs: (entries) => request("POST", "/logs/client", { entries }),
};
