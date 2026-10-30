"""In-memory store for development without Postgres and for tests.
Same interface as PgStore. Data is lost when the process stops."""
import copy
import os
import threading
from datetime import datetime, timezone

from app.graphs.common import profile_to_requirements, scrub_parsed, scrub_text
from app.graphs.fakes import FakeVectorStore

EMPTY_PROFILE = {"title": "", "required_skills": [], "preferred_skills": [], "min_experience_years": None,
                 "qualifications": [], "contact_email": "", "company": "", "remuneration": "", "start_date": "",
                 "address": "", "location_or_mode": "", "employment_type": "", "experience_level": "",
                 "application_deadline": "", "about_role": "", "responsibilities": [], "communication": "",
                 "other_criteria": [], "offers": [], "apply_documents": ""}
DEFAULT_WEIGHTS = {"technical_skills": 0.5, "experience": 0.3, "qualifications": 0.2}
LOCKED = ("open", "closed")      # once a job is open its requirements no longer change
SCREENED = ("screened", "interview_scheduled", "interviewed", "assessed")


class DuplicateApplication(Exception):
    """The same email was already processed."""


class DuplicateRecruiter(Exception):
    """An account with this email already exists."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def failure_reason_for(result: dict) -> str:
    """A reason safe to show. Never includes CV text or model output."""
    nodes = {e.get("node") for e in result.get("errors") or [] if isinstance(e, dict)}
    if nodes & {"extract_text", "ocr_fallback"}:
        return "The file could not be read. It may be scanned, protected or damaged."
    return "Screening could not be completed."


def _without_contact(result: dict) -> dict:
    """Keep the score and the job-relevant CV fields. Drop contact details."""
    kept = copy.deepcopy(result)
    kept.pop("contact", None)
    kept["parsed_cv"] = scrub_parsed(kept.get("parsed_cv"))
    for match in kept.get("matches") or []:
        match["justification"] = scrub_text(match.get("justification") or "")
    for score in kept.get("dimension_scores") or []:
        score["justification"] = scrub_text(score.get("justification") or "")
    return kept


def full_profile(partial: dict | None) -> dict:
    out = copy.deepcopy(EMPTY_PROFILE)
    out.update({k: v for k, v in (partial or {}).items() if k in EMPTY_PROFILE})
    return out


class MemoryStore:
    def __init__(self, apply_email: str = "applications@example.com", embedder=None):
        self.apply_email, self.embedder = apply_email, embedder
        self.items: dict[tuple[str, str], dict] = {}
        self.policies: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.decisions: list[dict] = []
        self.publish_results: list[dict] = []
        self.candidates: dict[str, dict] = {}
        self.applications: dict[str, dict] = {}
        self.cv_results: dict[tuple[str, str], dict] = {}
        self.score_cache: dict[tuple[str, str], dict] = {}
        self.audit: list[dict] = []
        self.recruiters: dict[str, dict] = {}
        self.interviews: dict[str, dict] = {}
        self.notes: dict[str, dict] = {}
        self.qa: dict[str, dict] = {}
        self.assessments: dict[tuple[str, str], dict] = {}
        self.daily_tokens: dict[str, int] = {}
        self._lock = threading.RLock()

    # ---- vectors ---------------------------------------------------------
    def _vec(self, text, task):
        """Real embeddings when an embedder is given (Gemini), otherwise a toy word-hash vector."""
        return self.embedder.embed([text], task)[0] if self.embedder else FakeVectorStore._vec(text)

    def index(self, collection, items):
        vecs = [self._vec(it["text"], "RETRIEVAL_DOCUMENT") for it in items]
        with self._lock:
            self.items = {k: v for k, v in self.items.items() if k[0] != collection}
            for it, vec in zip(items, vecs):
                self.items[(collection, it["id"])] = {**it, "owner": None, "vec": vec}

    def add(self, collection, owner_id, items):
        vecs = [self._vec(it["text"], "RETRIEVAL_DOCUMENT") for it in items]
        with self._lock:
            for it, vec in zip(items, vecs):
                self.items[(collection, it["id"])] = {**it, "owner": owner_id, "vec": vec}

    def _ranked(self, collection, vec, k):
        rows = [{"id": v["id"], "text": v["text"], "score": FakeVectorStore._cos(vec, v["vec"])}
                for key, v in self.items.items() if key[0] == collection]
        return sorted(rows, key=lambda r: r["score"], reverse=True)[:k]

    def search(self, collection, query, k):
        return self.search_many(collection, [query], k)[0]

    def search_many(self, collection, queries, k):
        if not queries:
            return []
        if self.embedder:
            vecs = self.embedder.embed(list(queries), "RETRIEVAL_QUERY")
        else:
            vecs = [FakeVectorStore._vec(q) for q in queries]
        with self._lock:
            return [self._ranked(collection, vec, k) for vec in vecs]

    def get_item(self, collection, item_id):
        v = self.items.get((collection, item_id))
        return {"id": v["id"], "text": v["text"]} if v else None

    # ---- policies --------------------------------------------------------
    def list_policies(self): return sorted(self.policies.values(), key=lambda p: p["effective_date"], reverse=True)
    def find_active_policy(self, title): return next((p for p in self.policies.values() if p["title"] == title and p["status"] == "active"), None)

    def publish_policy(self, policy, items, supersede_id=None):
        if supersede_id:
            self.items = {k: v for k, v in self.items.items() if v["owner"] != supersede_id}
            self.policies[supersede_id]["status"] = "superseded"
        self.policies[policy["id"]] = dict(policy)
        self.add("policy", policy["id"], items)

    def delete_policy(self, pid):
        self.policies.pop(pid, None)
        self.items = {k: v for k, v in self.items.items() if v["owner"] != pid}

    def policy_chunks(self, policy_id: str) -> list[dict]:
        rows = [v for (col, _), v in self.items.items() if col == "policy" and v.get("owner") == policy_id]
        return [{"id": v["id"], "text": v["text"]} for v in sorted(rows, key=lambda v: v["id"])]

    # ---- jobs ------------------------------------------------------------
    def create_job(self, job_id, owner_id=None):
        self.jobs[job_id] = {"id": job_id, "title": "New role", "status": "draft", "created_at": now(),
                             "owner_id": owner_id, "profile": full_profile(None), "requirements": [],
                             "weights": dict(DEFAULT_WEIGHTS), "post_text": None, "apply_email": None, "channels": []}

    def update_job(self, job_id, *, title=None, status=None, profile=None):
        j = self.jobs[job_id]
        locked = j["status"] in LOCKED
        if title and not locked: j["title"] = title
        if profile is not None and not locked:
            j["profile"] = full_profile(profile)
            j["requirements"] = profile_to_requirements(j["profile"]) if j["profile"]["required_skills"] else []
        if status: j["status"] = status

    def update_weights(self, job_id, weights): self.jobs[job_id]["weights"] = dict(weights)

    def _job_view(self, j):
        out = copy.deepcopy(j)
        out["applicants"] = sum(1 for c in self.candidates.values() if c["job_id"] == j["id"])
        return out

    def list_jobs(self): return [self._job_view(j) for j in sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)]
    def get_job(self, job_id): return self._job_view(self.jobs[job_id]) if job_id in self.jobs else None

    # Graph 0 repository methods
    def save_job_post(self, job_id, record):
        j = self.jobs[job_id]
        j["status"] = record["status"]
        if record["status"] == "open":
            j["post_text"] = record["final_text"]
            j["channels"] = [r["channel"] for r in record["publish_results"] if r["status"] == "ok"]
            j["apply_email"] = self.apply_email
            self.publish_results += [{"job_id": job_id, **r} for r in record["publish_results"]]

    def save_posting_decision(self, job_id, decision): self.decisions.append({"job_id": job_id, **decision})
    def mark_job_open(self, job_id): self.jobs[job_id]["status"] = "open"

    # ---- candidates and applications (F2) --------------------------------
    def create_candidate(self, c, application=None):
        """Atomic: the candidate and its application record are saved together or not at all."""
        if application:
            if application["message_id_hash"] in self.applications:
                raise DuplicateApplication
            self.applications[application["message_id_hash"]] = {**application, "job_id": c["job_id"], "candidate_id": c["id"], "status": "accepted"}
        self.candidates[c["id"]] = {
            "name": None, "email": None, "phone": None, "failure_reason": None, "aggregate_score": None,
            "status": "queued", "received_at": now(), "file_path": None, "photo_path": None,
            "face_embedding": None, "completion_requests": 0, "completion_thread_id": None,
            "completion_token": None, **c,
        }

    def update_candidate(self, cid: str, **fields) -> dict | None:
        allowed = {
            "name", "email", "phone", "status", "file_path", "photo_path", "face_embedding",
            "completion_requests", "completion_thread_id", "completion_token", "failure_reason",
            "aggregate_score",
        }
        c = self.candidates.get(cid)
        if not c:
            return None
        for key, value in fields.items():
            if key in allowed:
                c[key] = value
        return self.get_candidate(cid)

    def get_candidate_by_completion_token(self, token: str) -> dict | None:
        if not token:
            return None
        for c in self.candidates.values():
            if c.get("completion_token") == token:
                return self.get_candidate(c["id"])
        return None

    def get_candidate_by_completion_thread(self, message_id: str) -> dict | None:
        if not message_id:
            return None
        for c in self.candidates.values():
            if c.get("completion_thread_id") and c["completion_thread_id"] in message_id:
                return self.get_candidate(c["id"])
        return None

    def record_rejected_application(self, message_id_hash, job_id, reason) -> bool:
        if message_id_hash in self.applications:
            return False
        self.applications[message_id_hash] = {"message_id_hash": message_id_hash, "job_id": job_id, "candidate_id": None,
                                              "status": "rejected", "reject_reason": reason}
        return True

    def application_seen(self, message_id_hash): return message_id_hash in self.applications

    def record_completion_reply(self, message_id_hash: str, job_id: str, candidate_id: str) -> bool:
        """Mark a completeness-reply message as processed without creating a new candidate."""
        if message_id_hash in self.applications:
            return False
        self.applications[message_id_hash] = {
            "message_id_hash": message_id_hash, "job_id": job_id, "candidate_id": candidate_id, "status": "accepted",
        }
        return True

    def _cand_view(self, c):
        return {**{k: c[k] for k in ("id", "job_id", "name", "status", "aggregate_score", "source", "received_at")},
                "assessment_id": c.get("assessment_id")}

    def list_candidates(self, job_id=None):
        rows = [c for c in self.candidates.values() if not job_id or c["job_id"] == job_id]
        return [self._cand_view(c) for c in sorted(rows, key=lambda c: c["received_at"], reverse=True)]

    def get_candidate(self, cid):
        c = self.candidates.get(cid)
        if not c:
            return None
        return {
            **self._cand_view(c),
            "email": c["email"], "phone": c["phone"], "failure_reason": c["failure_reason"],
            "photo_path": c.get("photo_path"), "face_embedding": c.get("face_embedding"),
            "completion_requests": c.get("completion_requests") or 0,
            "completion_thread_id": c.get("completion_thread_id"),
            "completion_token": c.get("completion_token"),
            "file_path": c.get("file_path"),
        }

    def claim_candidate(self, cid):
        """Move a queued (or interrupted) candidate to processing. Returns the file the graph should read."""
        with self._lock:
            c = self.candidates.get(cid)
            if not c or c["status"] not in ("queued", "processing"):
                return None
            c["status"] = "processing"
            return {"id": cid, "job_id": c["job_id"], "file_path": c.get("file_path")}

    def list_pending_screening(self):
        rows = [c for c in self.candidates.values() if c["status"] in ("queued", "processing")]
        return [c["id"] for c in sorted(rows, key=lambda c: c["received_at"])]

    def save_cv_result(self, candidate_id, job_id, result):
        with self._lock:
            self._save_cv_result(candidate_id, job_id, result)

    def _save_cv_result(self, candidate_id, job_id, result):
        c = self.candidates.get(candidate_id)
        if not c or c["job_id"] != job_id:
            return
        if result.get("status") == "completed":
            c["aggregate_score"] = result.get("aggregate_score")
            c["failure_reason"] = None
            c["status"] = "screened"
            result = _without_contact(result)
        else:
            c["status"] = "failed"
            c["aggregate_score"] = None
            c["failure_reason"] = failure_reason_for(result)
        self.cv_results[(candidate_id, job_id)] = copy.deepcopy(result)

    def load_cv_result(self, candidate_id, job_id):
        found = self.cv_results.get((candidate_id, job_id))
        return copy.deepcopy(found) if found else None

    def load_score_cache(self, content_hash, rubric_key):
        with self._lock:
            found = self.score_cache.get((content_hash, rubric_key))
            return copy.deepcopy(found) if found else None

    def save_score_cache(self, content_hash, rubric_key, result):
        if result.get("status") != "completed":
            return
        with self._lock:
            self.score_cache[(content_hash, rubric_key)] = copy.deepcopy(result)

    def delete_candidate(self, cid):
        """Remove the candidate and everything linked to them. Returns the stored CV path (the caller deletes the file)."""
        c = self.candidates.pop(cid, None)
        if not c:
            return None
        self.applications = {h: a for h, a in self.applications.items() if a.get("candidate_id") != cid}
        self.items = {k: v for k, v in self.items.items() if k[0] != f"cv:{cid}"}
        self.cv_results = {k: v for k, v in self.cv_results.items() if k[0] != cid}
        for iid, row in list(self.interviews.items()):
            if row.get("candidate_id") != cid:
                continue
            _remove_file(row.get("transcript_file"))
            self.interviews.pop(iid, None)
            self.notes.pop(iid, None)
            self.qa.pop(iid, None)
        self.assessments = {k: v for k, v in self.assessments.items() if k[0] != cid}
        return {"file_path": c.get("file_path") or "", "photo_path": c.get("photo_path") or ""}

    # ---- recruiter accounts ----------------------------------------------
    def create_recruiter(self, row: dict) -> dict:
        email = row["email"]
        if email in self.recruiters:
            raise DuplicateRecruiter
        self.recruiters[email] = {"id": row["id"], "email": email, "name": row["name"],
                                  "password_hash": row["password_hash"], "role": row["role"], "created_at": now()}
        return {"id": row["id"], "email": email, "name": row["name"], "role": row["role"]}

    def get_recruiter_by_email(self, email: str) -> dict | None:
        found = self.recruiters.get(email)
        return copy.deepcopy(found) if found else None

    def get_recruiter(self, user_id: str) -> dict | None:
        for row in self.recruiters.values():
            if row["id"] == user_id:
                return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"]}
        return None

    # ---- audit and dashboard ---------------------------------------------
    def add_audit(self, actor_role, action, entity, entity_id, outcome="ok"):
        with self._lock:
            self.audit.append({"id": f"ev-{len(self.audit) + 1}", "ts": now(), "actor_role": actor_role, "action": action,
                               "entity": entity, "entity_id": entity_id, "outcome": outcome})

    def list_audit(self, limit=200): return list(reversed(self.audit))[:limit]

    def count_audit_since(self, action: str, entity_id: str, since_iso: str) -> int:
        with self._lock:
            return sum(1 for e in self.audit
                       if e["action"] == action and e["entity_id"] == entity_id and e["ts"] >= since_iso)

    def dashboard(self, owner_id=None):
        jobs = [j for j in self.jobs.values() if owner_id is None or j.get("owner_id") == owner_id]
        job_ids = {j["id"] for j in jobs}
        c = [x for x in self.candidates.values() if owner_id is None or x["job_id"] in job_ids]
        n = lambda statuses: sum(1 for x in c if x["status"] in statuses)
        return {"open_jobs": sum(1 for j in jobs if j["status"] == "open"), "applications_total": len(c),
                "awaiting_screening": n(("queued", "processing")), "failed_cvs": n(("failed",)),
                "interviews_scheduled": sum(1 for i in self.interviews.values() if i["status"] == "scheduled" and i["job_id"] in job_ids),
                "assessments_ready": 0,
                "pipeline": [{"stage": "Received", "count": len(c)}, {"stage": "Screened", "count": n(SCREENED)},
                             {"stage": "Interviewed", "count": n(("interviewed", "assessed"))}, {"stage": "Assessed", "count": n(("assessed",))}],
                "recent": self.list_audit(8)}

    def create_interview(self, row: dict) -> dict:
        with self._lock:
            self.interviews[row["id"]] = {
                "id": row["id"], "job_id": row["job_id"], "candidate_id": row["candidate_id"],
                "status": "scheduled", "join_token_hash": row["join_token_hash"],
                "scheduled_at": row["scheduled_at"], "created_at": now(),
                "started_at": None, "ended_at": None,
                "job_title": row["job_title"], "candidate_name": row.get("candidate_name") or "Candidate",
                "calendar_uid": row.get("calendar_uid") or f"{row['id']}@hirelens",
                "calendar_sequence": int(row.get("calendar_sequence") or 0),
                "transcript": None, "transcript_file": None, "qa_result": None,
                "copilot_calls": 0, "copilot_times": [], "eval_runs": 0,
            }
            cand = self.candidates.get(row["candidate_id"])
            if cand and cand["status"] == "screened":
                cand["status"] = "interview_scheduled"
            return copy.deepcopy(self.interviews[row["id"]])

    def list_interviews(self) -> list[dict]:
        with self._lock:
            rows = [copy.deepcopy(i) for i in self.interviews.values()]
        return sorted(rows, key=lambda i: i["created_at"], reverse=True)

    def get_interview(self, interview_id: str) -> dict | None:
        with self._lock:
            row = self.interviews.get(interview_id)
            return copy.deepcopy(row) if row else None

    def get_interview_by_token_hash(self, token_hash: str) -> dict | None:
        with self._lock:
            for row in self.interviews.values():
                if row["join_token_hash"] == token_hash:
                    return copy.deepcopy(row)
        return None

    def mark_interview_consent(self, interview_id: str) -> None:
        with self._lock:
            row = self.interviews.get(interview_id)
            if row:
                row["consent_at"] = now()

    def end_interview(self, interview_id: str) -> dict | None:
        with self._lock:
            row = self.interviews.get(interview_id)
            if not row:
                return None
            row["status"] = "completed"
            row["ended_at"] = now()
            cand = self.candidates.get(row["candidate_id"])
            if cand and cand["status"] != "assessed":
                cand["status"] = "interviewed"
            return copy.deepcopy(row)

    def reschedule_interview(self, interview_id: str, starts_at: str, token_hash: str) -> dict | None:
        with self._lock:
            row = self.interviews.get(interview_id)
            if not row or row["status"] == "completed":
                return None
            row["scheduled_at"] = starts_at
            row["join_token_hash"] = token_hash
            row["calendar_sequence"] = int(row.get("calendar_sequence") or 0) + 1
            return copy.deepcopy(row)

    def ensure_started(self, interview_id: str) -> str | None:
        with self._lock:
            row = self.interviews.get(interview_id)
            if not row:
                return None
            if not row.get("started_at"):
                row["started_at"] = now()
                if row["status"] == "scheduled":
                    row["status"] = "live"
            return row["started_at"]

    def append_turn(self, interview_id: str, turn: dict) -> dict | None:
        with self._lock:
            row = self.interviews.get(interview_id)
            if not row:
                return None
            doc = row.get("transcript") or {
                "interview_id": row["id"], "candidate_id": row["candidate_id"], "job_id": row["job_id"],
                "started_at": row.get("started_at"), "ended_at": None, "turns": [],
            }
            turn = dict(turn)
            turn["turn_id"] = f"t{len(doc['turns']) + 1:04d}"
            doc["turns"].append(turn)
            doc["started_at"] = row.get("started_at")
            row["transcript"] = doc
            return copy.deepcopy(turn)

    def edit_turn(self, interview_id: str, turn_id: str, text: str) -> dict | None:
        from app.services.language_guard import apply_language_guard

        with self._lock:
            row = self.interviews.get(interview_id)
            if not row or not row.get("transcript"):
                return None
            if row.get("status") == "completed":
                return None
            for turn in row["transcript"].get("turns") or []:
                if turn.get("turn_id") == turn_id:
                    if "original_text" not in turn:
                        turn["original_text"] = turn.get("text") or ""
                    turn["text"] = text
                    turn["edited"] = True
                    apply_language_guard(turn)
                    return copy.deepcopy(turn)
            return None

    def transcript_document(self, interview_id: str) -> dict | None:
        with self._lock:
            row = self.interviews.get(interview_id)
            if not row:
                return None
            doc = row.get("transcript") or {
                "interview_id": row["id"], "candidate_id": row["candidate_id"], "job_id": row["job_id"],
                "started_at": row.get("started_at"), "ended_at": row.get("ended_at"), "turns": [],
            }
            return copy.deepcopy(doc)

    def save_transcript(self, interview_id: str, doc: dict, file_path: str | None) -> None:
        with self._lock:
            row = self.interviews.get(interview_id)
            if not row:
                return
            row["transcript"] = copy.deepcopy(doc)
            row["transcript_file"] = file_path

    def load_transcript(self, session_id: str) -> list[dict]:
        doc = self.transcript_document(session_id)
        return list(doc["turns"]) if doc else []

    def save_qa_result(self, session_id: str, result: dict) -> None:
        with self._lock:
            self.qa[session_id] = copy.deepcopy(result)
            row = self.interviews.get(session_id)
            if row:
                row["qa_result"] = copy.deepcopy(result)

    def load_qa_result(self, session_id: str) -> dict | None:
        with self._lock:
            found = self.qa.get(session_id)
            return copy.deepcopy(found) if found else None

    def save_assessment(self, candidate_id: str, session_id: str, report: dict) -> None:
        with self._lock:
            stored = copy.deepcopy(report)
            stored["id"] = stored.get("id") or f"asm-{session_id}"
            stored.setdefault("created_at", now())
            self.assessments[(candidate_id, session_id)] = stored
            cand = self.candidates.get(candidate_id)
            if cand:
                cand["assessment_id"] = stored["id"]
                cand["status"] = "assessed"

    def load_assessment(self, assessment_id: str) -> dict | None:
        with self._lock:
            for report in self.assessments.values():
                if report.get("id") == assessment_id:
                    return copy.deepcopy(report)
        return None

    def load_assessment_for_candidate(self, candidate_id: str) -> dict | None:
        with self._lock:
            found = [r for (cid, _), r in self.assessments.items() if cid == candidate_id]
        return copy.deepcopy(found[-1]) if found else None

    def list_assessments(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(r) for r in self.assessments.values()]

    def save_note(self, interview_id: str, note: dict) -> dict:
        with self._lock:
            self.notes[interview_id] = copy.deepcopy(note)
            return copy.deepcopy(note)

    def get_note(self, interview_id: str) -> dict | None:
        with self._lock:
            found = self.notes.get(interview_id)
            return copy.deepcopy(found) if found else None

    def list_notes(self, candidate_id: str) -> list[dict]:
        with self._lock:
            rows = [n for n in self.notes.values() if n.get("candidate_id") == candidate_id]
        return [copy.deepcopy(n) for n in rows]

    def begin_copilot(self, interview_id: str, *, max_calls: int, per_minute: int, now_ts: float) -> tuple[str | None, int]:
        """Reserve one co-pilot call. Returns (error code or None, calls remaining after this one)."""
        with self._lock:
            row = self.interviews.get(interview_id)
            if not row:
                return "missing", 0
            times = [t for t in row.get("copilot_times") or [] if now_ts - t < 60]
            used = int(row.get("copilot_calls") or 0)
            if used >= max_calls:
                row["copilot_times"] = times
                return "calls", 0
            if len(times) >= per_minute:
                row["copilot_times"] = times
                return "minute", max_calls - used
            row["copilot_calls"] = used + 1
            times.append(now_ts)
            row["copilot_times"] = times
            return None, max_calls - (used + 1)

    def copilot_remaining(self, interview_id: str, max_calls: int) -> int:
        with self._lock:
            row = self.interviews.get(interview_id)
            if not row:
                return 0
            return max(0, max_calls - int(row.get("copilot_calls") or 0))

    def try_spend_tokens(self, day: str, amount: int, budget: int) -> bool:
        with self._lock:
            used = int(self.daily_tokens.get(day, 0))
            if amount > 0 and used + amount > budget:
                return False
            self.daily_tokens[day] = max(0, used + amount)
            return True

    def claim_eval_run(self, interview_id: str, limit: int) -> bool:
        with self._lock:
            row = self.interviews.get(interview_id)
            if not row or int(row.get("eval_runs") or 0) >= limit:
                return False
            row["eval_runs"] = int(row.get("eval_runs") or 0) + 1
            return True


def _remove_file(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        return
