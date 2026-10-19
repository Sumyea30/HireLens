"""PostgreSQL + pgvector store using the v2 schema (app/db/schema.sql).

One short-lived connection per operation keeps this simple. TODO: use a connection pool.
Vector search is a sequential scan, fine at demo scale (see the HNSW note in schema.sql).
Same interface as MemoryStore.
"""
import json
import os
import pathlib

import psycopg
from psycopg.rows import dict_row

from app.graphs.common import profile_to_requirements, scrub_parsed, scrub_text
from app.services.memory_store import (DEFAULT_WEIGHTS, LOCKED, SCREENED, DuplicateApplication, DuplicateRecruiter, failure_reason_for, full_profile)

SCHEMA_FILE = pathlib.Path(__file__).resolve().parent.parent / "db" / "schema.sql"
OWNER_COLUMNS = {"policy": "policy_id", "cv": "candidate_id", "req": "job_id"}


def _vec(values: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in values) + "]"


def _owner(collection: str, doc_id: str | None) -> tuple[str, str]:
    """Which table owns the vectors of a collection. Deleting the owner deletes its vectors."""
    kind, _, rest = collection.partition(":")
    if collection == "policy":
        return OWNER_COLUMNS["policy"], doc_id or ""
    if kind in ("cv", "req") and rest:
        return OWNER_COLUMNS[kind], rest
    raise ValueError("unknown collection")


def _iso(row: dict, *keys: str) -> dict:
    return {**row, **{k: row[k].isoformat() for k in keys if row.get(k)}}


class PgStore:
    def __init__(self, url: str, embedder, dims: int = 768, apply_email: str = "applications@example.com"):
        self.url, self.embedder, self.dims, self.apply_email = url, embedder, dims, apply_email

    def _conn(self):
        return psycopg.connect(self.url, autocommit=True, row_factory=dict_row)

    def init_schema(self) -> None:
        sql = SCHEMA_FILE.read_text().replace("vector(768)", f"vector({self.dims})")
        with self._conn() as c:
            old = c.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                            "AND table_name = 'vector_item' AND column_name = 'doc_id'").fetchone()
            if old:
                raise RuntimeError("An old (v1) database was found. It only holds demo data: reset it with "
                                   "'docker compose down -v' and start again.")
            c.execute(sql)

    # ---- vectors ---------------------------------------------------------
    def _embed(self, items):
        return self.embedder.embed([i["text"] for i in items], "RETRIEVAL_DOCUMENT") if items else []

    @staticmethod
    def _insert(c, collection, column, owner_id, items, vecs):
        for it, v in zip(items, vecs):
            c.execute(f"INSERT INTO vector_item (collection, item_id, {column}, text, embedding) VALUES (%s,%s,%s,%s,%s::vector) "
                      "ON CONFLICT (collection, item_id) DO UPDATE SET text = EXCLUDED.text, embedding = EXCLUDED.embedding",
                      (collection, it["id"], owner_id, it["text"], _vec(v)))

    def index(self, collection, items):
        """Replace a whole collection (per-candidate CV chunks, per-job requirement vectors)."""
        column, owner_id = _owner(collection, None)
        vecs = self._embed(items)
        with self._conn() as c, c.transaction():
            c.execute("DELETE FROM vector_item WHERE collection = %s", (collection,))
            self._insert(c, collection, column, owner_id, items, vecs)

    def add(self, collection, owner_id, items):
        column, owner = _owner(collection, owner_id)
        vecs = self._embed(items)
        with self._conn() as c, c.transaction():
            self._insert(c, collection, column, owner, items, vecs)

    def search(self, collection, query, k):
        return self.search_many(collection, [query], k)[0]

    def search_many(self, collection, queries, k):
        """Embed every query in one request, then search. One CV no longer waits on each requirement."""
        if not queries:
            return []
        vecs = self.embedder.embed(list(queries), "RETRIEVAL_QUERY")
        out = []
        with self._conn() as c:
            for vec in vecs:
                q = _vec(vec)
                rows = c.execute(
                    "SELECT item_id AS id, text, 1 - (embedding <=> %s::vector) AS score FROM vector_item "
                    "WHERE collection = %s ORDER BY embedding <=> %s::vector LIMIT %s", (q, collection, q, k)).fetchall()
                out.append([{"id": r["id"], "text": r["text"], "score": float(r["score"])} for r in rows])
        return out

    def get_item(self, collection, item_id):
        with self._conn() as c:
            r = c.execute("SELECT item_id AS id, text FROM vector_item WHERE collection=%s AND item_id=%s", (collection, item_id)).fetchone()
        return dict(r) if r else None

    # ---- policies --------------------------------------------------------
    def list_policies(self):
        with self._conn() as c:
            return [_iso(r, "effective_date") for r in c.execute("SELECT * FROM policy_document ORDER BY effective_date DESC").fetchall()]

    def find_active_policy(self, title):
        with self._conn() as c:
            r = c.execute("SELECT * FROM policy_document WHERE title=%s AND status='active'", (title,)).fetchone()
        return dict(r) if r else None

    def publish_policy(self, policy, items, supersede_id=None):
        """Embed first (a failure leaves the old version active), then swap versions in one transaction."""
        vecs = self._embed(items)
        with self._conn() as c, c.transaction():
            if supersede_id:
                c.execute("DELETE FROM vector_item WHERE policy_id = %s", (supersede_id,))
                c.execute("UPDATE policy_document SET status = 'superseded' WHERE id = %s", (supersede_id,))
            c.execute("INSERT INTO policy_document (id,title,version,effective_date,status,chunks) VALUES (%s,%s,%s,%s,%s,%s)",
                      (policy["id"], policy["title"], policy["version"], policy["effective_date"], policy["status"], policy["chunks"]))
            self._insert(c, "policy", "policy_id", policy["id"], items, vecs)

    def delete_policy(self, pid):
        with self._conn() as c:
            c.execute("DELETE FROM policy_document WHERE id = %s", (pid,))     # chunks go with it (ON DELETE CASCADE)

    def policy_chunks(self, policy_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT item_id AS id, text FROM vector_item WHERE collection = 'policy' AND policy_id = %s ORDER BY item_id",
                (policy_id,)).fetchall()
        return [{"id": r["id"], "text": r["text"]} for r in rows]

    # ---- jobs ------------------------------------------------------------
    def create_job(self, job_id, owner_id=None):
        with self._conn() as c:
            c.execute("INSERT INTO job (id, weights, owner_id) VALUES (%s, %s::jsonb, %s)",
                      (job_id, json.dumps(DEFAULT_WEIGHTS), owner_id))

    def update_job(self, job_id, *, title=None, status=None, profile=None):
        with self._conn() as c, c.transaction():
            cur = c.execute("SELECT status FROM job WHERE id = %s FOR UPDATE", (job_id,)).fetchone()
            if not cur:
                return
            if cur["status"] not in LOCKED:        # an open job keeps the requirements candidates were screened against
                if title:
                    c.execute("UPDATE job SET title = %s WHERE id = %s", (title, job_id))
                if profile is not None:
                    p = full_profile(profile)
                    c.execute("UPDATE job SET profile = %s::jsonb WHERE id = %s", (json.dumps(p), job_id))
                    c.execute("DELETE FROM job_requirement WHERE job_id = %s", (job_id,))
                    reqs = profile_to_requirements(p) if p["required_skills"] else []
                    for i, r in enumerate(reqs, start=1):
                        c.execute("INSERT INTO job_requirement (job_id, req_id, position, text, kind, category) VALUES (%s,%s,%s,%s,%s,%s)",
                                  (job_id, r["id"], i, r["text"], r["kind"], r["category"]))
            if status:
                c.execute("UPDATE job SET status = %s WHERE id = %s", (status, job_id))

    def update_weights(self, job_id, weights):
        with self._conn() as c:
            c.execute("UPDATE job SET weights = %s::jsonb WHERE id = %s", (json.dumps(weights), job_id))

    def _jobs(self, where: str, args: tuple):
        with self._conn() as c:
            rows = c.execute(f"SELECT j.*, (SELECT count(*) FROM candidate k WHERE k.job_id = j.id) AS applicants "
                             f"FROM job j {where} ORDER BY j.created_at DESC", args).fetchall()
            reqs = c.execute("SELECT job_id, req_id AS id, text, kind, category FROM job_requirement "
                             "WHERE job_id = ANY(%s) ORDER BY job_id, position", ([r["id"] for r in rows],)).fetchall()
        by_job: dict[str, list] = {}
        for r in reqs:
            by_job.setdefault(r["job_id"], []).append({k: r[k] for k in ("id", "text", "kind", "category")})
        return [{**_iso(r, "created_at", "closed_at"), "profile": full_profile(r["profile"]), "requirements": by_job.get(r["id"], [])} for r in rows]

    def list_jobs(self): return self._jobs("", ())
    def get_job(self, job_id):
        rows = self._jobs("WHERE j.id = %s", (job_id,))
        return rows[0] if rows else None

    # Graph 0 repository methods
    def save_job_post(self, job_id, record):
        with self._conn() as c, c.transaction():
            if record["status"] == "open":
                results = record["publish_results"]
                c.execute("UPDATE job SET status='open', post_text=%s, channels=%s::jsonb, apply_email=%s WHERE id=%s",
                          (record["final_text"], json.dumps([r["channel"] for r in results if r["status"] == "ok"]), self.apply_email, job_id))
                for r in results:
                    c.execute("INSERT INTO publish_result (job_id, channel, status, public_url, error_code) VALUES (%s,%s,%s,%s,%s)",
                              (job_id, r["channel"], r["status"], r.get("url"), (r.get("error") or None) and str(r["error"])[:200]))
            else:
                c.execute("UPDATE job SET status=%s WHERE id=%s", (record["status"], job_id))

    def save_posting_decision(self, job_id, decision):
        with self._conn() as c:
            c.execute("INSERT INTO posting_decision (job_id, action, channels, edited, approver_role) VALUES (%s,%s,%s::jsonb,%s,%s)",
                      (job_id, decision["action"], json.dumps(decision.get("channels", [])), bool(decision.get("edited_post")),
                       decision.get("approver_role", "interviewer")))

    def mark_job_open(self, job_id):
        with self._conn() as c:
            c.execute("UPDATE job SET status='open' WHERE id=%s", (job_id,))

    # ---- candidates and applications (F2) --------------------------------
    def create_candidate(self, c_, application=None):
        """Atomic: the candidate and its application record are saved together or not at all."""
        with self._conn() as c, c.transaction():
            c.execute("INSERT INTO candidate (id, job_id, email, status, source, file_path) VALUES (%s,%s,%s,'queued',%s,%s)",
                      (c_["id"], c_["job_id"], c_.get("email"), c_["source"], c_.get("file_path")))
            if application:
                row = c.execute("INSERT INTO application (job_id, candidate_id, message_id_hash, status) VALUES (%s,%s,%s,'accepted') "
                                "ON CONFLICT (message_id_hash) DO NOTHING RETURNING id",
                                (c_["job_id"], c_["id"], application["message_id_hash"])).fetchone()
                if not row:
                    raise DuplicateApplication      # leaving the block rolls the candidate back

    def record_rejected_application(self, message_id_hash, job_id, reason) -> bool:
        with self._conn() as c:
            row = c.execute("INSERT INTO application (job_id, message_id_hash, status, reject_reason) VALUES (%s,%s,'rejected',%s) "
                            "ON CONFLICT (message_id_hash) DO NOTHING RETURNING id", (job_id, message_id_hash, reason)).fetchone()
        return bool(row)

    def application_seen(self, message_id_hash):
        with self._conn() as c:
            return bool(c.execute("SELECT 1 FROM application WHERE message_id_hash = %s", (message_id_hash,)).fetchone())

    _CAND = ("SELECT c.id, c.job_id, c.name, c.status, c.aggregate_score::float AS aggregate_score, c.source, c.received_at, "
             "a.id AS assessment_id, c.email, c.phone, c.failure_reason FROM candidate c LEFT JOIN assessment a ON a.candidate_id = c.id")

    def list_candidates(self, job_id=None):
        where, args = ("WHERE c.job_id = %s", (job_id,)) if job_id else ("", ())
        with self._conn() as c:
            rows = c.execute(f"{self._CAND} {where} ORDER BY c.received_at DESC", args).fetchall()
        return [_iso({k: r[k] for k in ("id", "job_id", "name", "status", "aggregate_score", "source", "received_at", "assessment_id")}, "received_at") for r in rows]

    def get_candidate(self, cid):
        with self._conn() as c:
            r = c.execute(f"{self._CAND} WHERE c.id = %s", (cid,)).fetchone()
        return _iso(r, "received_at") if r else None

    def claim_candidate(self, cid):
        """Move a queued (or interrupted) candidate to processing. Returns the file the graph should read."""
        with self._conn() as c:
            r = c.execute("UPDATE candidate SET status = 'processing' WHERE id = %s AND status IN ('queued', 'processing') "
                          "RETURNING id, job_id, file_path", (cid,)).fetchone()
        return dict(r) if r else None

    def list_pending_screening(self):
        with self._conn() as c:
            rows = c.execute("SELECT id FROM candidate WHERE status IN ('queued', 'processing') ORDER BY received_at").fetchall()
        return [r["id"] for r in rows]

    def save_cv_result(self, candidate_id, job_id, result):
        """Persist Graph A output: parsed CV, requirement matches, dimension scores, and the new status."""
        with self._conn() as c, c.transaction():
            row = c.execute("SELECT id FROM candidate WHERE id = %s AND job_id = %s FOR UPDATE", (candidate_id, job_id)).fetchone()
            if not row:
                return
            c.execute("DELETE FROM screening_result WHERE candidate_id = %s", (candidate_id,))
            c.execute("DELETE FROM score WHERE candidate_id = %s", (candidate_id,))
            c.execute("DELETE FROM cv_parsed WHERE candidate_id = %s", (candidate_id,))
            if result.get("status") != "completed":
                c.execute("UPDATE candidate SET status = 'failed', aggregate_score = NULL, failure_reason = %s WHERE id = %s",
                          (failure_reason_for(result), candidate_id))
                return
            version = result.get("prompt_version") or "cv-v1"
            c.execute("UPDATE candidate SET status = 'screened', aggregate_score = %s, failure_reason = NULL WHERE id = %s",
                      (result.get("aggregate_score"), candidate_id))
            c.execute("INSERT INTO cv_parsed (candidate_id, parsed, prompt_version) VALUES (%s, %s::jsonb, %s)",
                      (candidate_id, json.dumps(scrub_parsed(result.get("parsed_cv"))), version))
            for m in result.get("matches") or []:
                c.execute("INSERT INTO screening_result (candidate_id, job_id, requirement_id, status, justification, evidence_refs, prompt_version) "
                          "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
                          (candidate_id, job_id, m["requirement_id"], m["status"], scrub_text(m.get("justification") or ""),
                           json.dumps(m.get("evidence_refs") or []), version))
            weights = result.get("weights") or {}
            for s in result.get("dimension_scores") or []:
                weight = round(float(s.get("weight", weights.get(s["dimension"], 0)) or 0), 3)
                c.execute("INSERT INTO score (candidate_id, dimension, value, weight, justification, evidence_refs, prompt_version) "
                          "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
                          (candidate_id, s["dimension"], s["score"], weight, scrub_text(s.get("justification") or ""),
                           json.dumps(s.get("evidence_refs") or []), version))

    def load_cv_result(self, candidate_id, job_id):
        with self._conn() as c:
            cand = c.execute("SELECT status, aggregate_score::float AS aggregate_score FROM candidate WHERE id = %s AND job_id = %s",
                             (candidate_id, job_id)).fetchone()
            if not cand or cand["status"] in ("queued", "processing"):
                return None
            if cand["status"] == "failed":
                return {"status": "failed", "candidate_id": candidate_id, "job_id": job_id}
            parsed = c.execute("SELECT parsed, prompt_version FROM cv_parsed WHERE candidate_id = %s", (candidate_id,)).fetchone()
            if not parsed:
                return None
            matches = c.execute("SELECT requirement_id, status, justification, evidence_refs FROM screening_result WHERE candidate_id = %s",
                                (candidate_id,)).fetchall()
            scores = c.execute("SELECT dimension, value::float AS value, weight::float AS weight, justification, evidence_refs "
                               "FROM score WHERE candidate_id = %s", (candidate_id,)).fetchall()
        return {
            "status": "completed", "candidate_id": candidate_id, "job_id": job_id,
            "aggregate_score": cand["aggregate_score"], "parsed_cv": parsed["parsed"], "prompt_version": parsed["prompt_version"],
            "matches": [{"requirement_id": m["requirement_id"], "status": m["status"], "justification": m["justification"],
                         "evidence_refs": list(m["evidence_refs"] or [])} for m in matches],
            "dimension_scores": [{"dimension": s["dimension"], "score": s["value"], "weight": s["weight"],
                                  "justification": s["justification"], "evidence_refs": list(s["evidence_refs"] or [])} for s in scores],
            "weights": {s["dimension"]: s["weight"] for s in scores},
        }

    def load_score_cache(self, content_hash, rubric_key):
        with self._conn() as c:
            row = c.execute("SELECT result FROM screening_cache WHERE content_hash = %s AND rubric_key = %s",
                            (content_hash, rubric_key)).fetchone()
        if not row:
            return None
        result = row["result"]
        if isinstance(result, str):
            result = json.loads(result)
        return dict(result)

    def save_score_cache(self, content_hash, rubric_key, result):
        if result.get("status") != "completed":
            return
        with self._conn() as c:
            c.execute("INSERT INTO screening_cache (content_hash, rubric_key, result) VALUES (%s, %s, %s::jsonb) "
                      "ON CONFLICT (content_hash, rubric_key) DO NOTHING",
                      (content_hash, rubric_key, json.dumps(result)))

    def delete_candidate(self, cid):
        """Delete the candidate; every dependent row goes with it (ON DELETE CASCADE). Returns the CV file path to remove."""
        with self._conn() as c, c.transaction():
            r = c.execute("SELECT file_path FROM candidate WHERE id = %s", (cid,)).fetchone()
            if not r:
                return None
            files = c.execute("SELECT transcript_file FROM interview_session WHERE candidate_id = %s", (cid,)).fetchall()
            c.execute("DELETE FROM candidate WHERE id = %s", (cid,))
        for row in files:
            _remove_file(row["transcript_file"])
        return r["file_path"] or ""

    def get_recruiter(self, user_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT id, email, name, role FROM recruiter WHERE id = %s", (user_id,)).fetchone()
        return dict(r) if r else None

    # ---- recruiter accounts ----------------------------------------------
    def create_recruiter(self, row: dict) -> dict:
        with self._conn() as c:
            try:
                c.execute("INSERT INTO recruiter (id, email, name, password_hash, role) VALUES (%s,%s,%s,%s,%s)",
                          (row["id"], row["email"], row["name"], row["password_hash"], row["role"]))
            except psycopg.errors.UniqueViolation:
                raise DuplicateRecruiter from None
        return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"]}

    def get_recruiter_by_email(self, email: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT id, email, name, password_hash, role FROM recruiter WHERE email = %s", (email,)).fetchone()
        return dict(r) if r else None

    # ---- audit and dashboard ---------------------------------------------
    def add_audit(self, actor_role, action, entity, entity_id, outcome="ok"):
        with self._conn() as c:
            c.execute("INSERT INTO audit_event (actor_role, action, entity, entity_id, outcome) VALUES (%s,%s,%s,%s,%s)",
                      (actor_role, action, entity, entity_id, outcome))

    def list_audit(self, limit=200):
        with self._conn() as c:
            rows = c.execute("SELECT id::text AS id, ts, actor_role, action, entity, entity_id, outcome FROM audit_event "
                             "ORDER BY ts DESC, id DESC LIMIT %s", (limit,)).fetchall()
        return [_iso(r, "ts") for r in rows]

    def dashboard(self, owner_id=None):
        with self._conn() as c:
            # A null owner means every job. The cast lets Postgres type that parameter.
            own = (owner_id, owner_id)
            counts = {r["status"]: r["n"] for r in c.execute(
                "SELECT c.status, count(*) AS n FROM candidate c JOIN job j ON j.id = c.job_id "
                "WHERE %s::text IS NULL OR j.owner_id = %s GROUP BY c.status", own).fetchall()}
            open_jobs = c.execute("SELECT count(*) AS n FROM job WHERE status = 'open' AND (%s::text IS NULL OR owner_id = %s)", own).fetchone()["n"]
            scheduled = c.execute(
                "SELECT count(*) AS n FROM interview_session s JOIN job j ON j.id = s.job_id "
                "WHERE s.status = 'scheduled' AND (%s::text IS NULL OR j.owner_id = %s)", own).fetchone()["n"]
            assessed = c.execute(
                "SELECT count(*) AS n FROM assessment a JOIN candidate cand ON cand.id = a.candidate_id "
                "JOIN job j ON j.id = cand.job_id WHERE %s::text IS NULL OR j.owner_id = %s", own).fetchone()["n"]
        n = lambda statuses: sum(counts.get(s, 0) for s in statuses)
        total = sum(counts.values())
        return {"open_jobs": open_jobs, "applications_total": total, "awaiting_screening": n(("queued", "processing")),
                "failed_cvs": n(("failed",)), "interviews_scheduled": scheduled, "assessments_ready": assessed,
                "pipeline": [{"stage": "Received", "count": total}, {"stage": "Screened", "count": n(SCREENED)},
                             {"stage": "Interviewed", "count": n(("interviewed", "assessed"))}, {"stage": "Assessed", "count": n(("assessed",))}],
                "recent": self.list_audit(8)}

    def create_interview(self, row: dict) -> dict:
        with self._conn() as c, c.transaction():
            c.execute(
                "INSERT INTO interview_session (id, job_id, candidate_id, status, join_token_hash, scheduled_at, calendar_uid, calendar_sequence) "
                "VALUES (%s,%s,%s,'scheduled',%s,%s,%s,%s)",
                (row["id"], row["job_id"], row["candidate_id"], row["join_token_hash"], row["scheduled_at"],
                 row.get("calendar_uid") or f"{row['id']}@hirelens", int(row.get("calendar_sequence") or 0)))
            c.execute("UPDATE candidate SET status = 'interview_scheduled' WHERE id = %s AND status = 'screened'", (row["candidate_id"],))
        found = self.get_interview(row["id"])
        return found

    def list_interviews(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT s.id, s.job_id, j.title AS job_title, s.candidate_id, "
                "COALESCE(cand.name, 'Candidate') AS candidate_name, s.status, s.created_at, "
                "s.scheduled_at, s.started_at, s.ended_at "
                "FROM interview_session s JOIN job j ON j.id = s.job_id "
                "JOIN candidate cand ON cand.id = s.candidate_id "
                "ORDER BY s.created_at DESC").fetchall()
        return [_iso(dict(r), "created_at", "scheduled_at", "started_at", "ended_at") for r in rows]

    def get_interview(self, interview_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT s.id, s.job_id, j.title AS job_title, s.candidate_id, "
                "COALESCE(cand.name, 'Candidate') AS candidate_name, s.status, s.created_at, "
                "s.scheduled_at, s.started_at, s.ended_at, s.join_token_hash, s.calendar_uid, s.calendar_sequence "
                "FROM interview_session s JOIN job j ON j.id = s.job_id "
                "JOIN candidate cand ON cand.id = s.candidate_id WHERE s.id = %s", (interview_id,)).fetchone()
        return _iso(dict(r), "created_at", "scheduled_at", "started_at", "ended_at") if r else None

    def get_interview_by_token_hash(self, token_hash: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT id FROM interview_session WHERE join_token_hash = %s", (token_hash,)).fetchone()
        return self.get_interview(r["id"]) if r else None

    def mark_interview_consent(self, interview_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE interview_session SET consent_at = now(), consent_version = '1' WHERE id = %s", (interview_id,))

    def end_interview(self, interview_id: str) -> dict | None:
        with self._conn() as c, c.transaction():
            row = c.execute("UPDATE interview_session SET status = 'completed', ended_at = now() WHERE id = %s RETURNING candidate_id",
                            (interview_id,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE candidate SET status = 'interviewed' WHERE id = %s AND status <> 'assessed'", (row["candidate_id"],))
        return self.get_interview(interview_id)

    def reschedule_interview(self, interview_id: str, starts_at: str, token_hash: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "UPDATE interview_session SET scheduled_at = %s, join_token_hash = %s, calendar_sequence = calendar_sequence + 1 "
                "WHERE id = %s AND status <> 'completed' RETURNING id",
                (starts_at, token_hash, interview_id)).fetchone()
        return self.get_interview(interview_id) if row else None

    def ensure_started(self, interview_id: str) -> str | None:
        with self._conn() as c:
            c.execute(
                "UPDATE interview_session SET started_at = COALESCE(started_at, now()), "
                "status = CASE WHEN status = 'scheduled' THEN 'live' ELSE status END WHERE id = %s",
                (interview_id,))
        row = self.get_interview(interview_id)
        return row.get("started_at") if row else None

    def append_turn(self, interview_id: str, turn: dict) -> dict | None:
        with self._conn() as c, c.transaction():
            row = c.execute(
                "SELECT id, candidate_id, job_id, started_at, transcript FROM interview_session WHERE id = %s FOR UPDATE",
                (interview_id,)).fetchone()
            if not row:
                return None
            doc = row["transcript"] if isinstance(row["transcript"], dict) else None
            if doc is None and isinstance(row["transcript"], str):
                doc = json.loads(row["transcript"])
            if not doc:
                doc = {"interview_id": row["id"], "candidate_id": row["candidate_id"], "job_id": row["job_id"],
                       "started_at": row["started_at"].isoformat() if row["started_at"] else None, "ended_at": None, "turns": []}
            turn = dict(turn)
            turn["turn_id"] = f"t{len(doc['turns']) + 1:04d}"
            doc["turns"].append(turn)
            c.execute("UPDATE interview_session SET transcript = %s::jsonb WHERE id = %s", (json.dumps(doc), interview_id))
        return turn

    def transcript_document(self, interview_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, candidate_id, job_id, started_at, ended_at, transcript FROM interview_session WHERE id = %s",
                (interview_id,)).fetchone()
        if not row:
            return None
        doc = row["transcript"]
        if isinstance(doc, str):
            doc = json.loads(doc)
        if not doc:
            return {"interview_id": row["id"], "candidate_id": row["candidate_id"], "job_id": row["job_id"],
                    "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                    "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None, "turns": []}
        return doc

    def save_transcript(self, interview_id: str, doc: dict, file_path: str | None) -> None:
        with self._conn() as c:
            c.execute("UPDATE interview_session SET transcript = %s::jsonb, transcript_file = %s WHERE id = %s",
                      (json.dumps(doc), file_path, interview_id))

    def load_transcript(self, session_id: str) -> list[dict]:
        doc = self.transcript_document(session_id)
        return list(doc["turns"]) if doc else []

    def save_qa_result(self, session_id: str, result: dict) -> None:
        with self._conn() as c:
            c.execute("UPDATE interview_session SET qa_result = %s::jsonb WHERE id = %s", (json.dumps(result), session_id))

    def load_qa_result(self, session_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT qa_result FROM interview_session WHERE id = %s", (session_id,)).fetchone()
        if not row or row["qa_result"] is None:
            return None
        return json.loads(row["qa_result"]) if isinstance(row["qa_result"], str) else row["qa_result"]

    def save_assessment(self, candidate_id: str, session_id: str, report: dict) -> None:
        stored = dict(report)
        stored["id"] = stored.get("id") or f"asm-{session_id}"
        scores = stored.get("component_scores") or {}
        status = stored.get("status") if stored.get("status") in ("completed", "partial") else "completed"
        position = stored.get("overall_position") if stored.get("overall_position") in ("strong", "promising", "uncertain", "weak") else "uncertain"
        with self._conn() as c, c.transaction():
            c.execute(
                "INSERT INTO assessment (id, candidate_id, session_id, status, has_interview, overall_position, "
                "cv_score, interview_score, combined, findings, prompt_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
                "ON CONFLICT (candidate_id) DO UPDATE SET session_id = EXCLUDED.session_id, status = EXCLUDED.status, "
                "has_interview = EXCLUDED.has_interview, overall_position = EXCLUDED.overall_position, cv_score = EXCLUDED.cv_score, "
                "interview_score = EXCLUDED.interview_score, combined = EXCLUDED.combined, findings = EXCLUDED.findings, "
                "prompt_version = EXCLUDED.prompt_version, id = EXCLUDED.id",
                (stored["id"], candidate_id, session_id if stored.get("has_interview") else None, status,
                 bool(stored.get("has_interview")), position, scores.get("cv_score") or 0, scores.get("interview_score"),
                 scores.get("combined") or 0, json.dumps(stored), stored.get("prompt_version") or "assessment-v1"))
            c.execute("UPDATE candidate SET status = 'assessed' WHERE id = %s", (candidate_id,))

    def load_assessment(self, assessment_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT findings FROM assessment WHERE id = %s", (assessment_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["findings"]) if isinstance(row["findings"], str) else row["findings"]

    def load_assessment_for_candidate(self, candidate_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT id, findings FROM assessment WHERE candidate_id = %s", (candidate_id,)).fetchone()
        if not row:
            return None
        report = json.loads(row["findings"]) if isinstance(row["findings"], str) else row["findings"]
        report["id"] = report.get("id") or row["id"]
        return report

    def list_assessments(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, findings FROM assessment ORDER BY created_at DESC").fetchall()
        out = []
        for row in rows:
            report = json.loads(row["findings"]) if isinstance(row["findings"], str) else dict(row["findings"])
            report["id"] = report.get("id") or row["id"]
            out.append(report)
        return out

    def save_note(self, interview_id: str, note: dict) -> dict:
        with self._conn() as c:
            c.execute(
                "INSERT INTO interview_note (session_id, candidate_id, author_id, author_name, body, markers, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s::jsonb, now()) ON CONFLICT (session_id) DO UPDATE SET "
                "author_id = EXCLUDED.author_id, author_name = EXCLUDED.author_name, body = EXCLUDED.body, "
                "markers = EXCLUDED.markers, updated_at = now()",
                (interview_id, note["candidate_id"], note["author_id"], note["author_name"], note["body"], json.dumps(note.get("markers") or [])))
        return self.get_note(interview_id) or note

    def get_note(self, interview_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT session_id, candidate_id, author_id, author_name, body, markers, updated_at FROM interview_note WHERE session_id = %s",
                (interview_id,)).fetchone()
        if not row:
            return None
        note = dict(row)
        if isinstance(note["markers"], str):
            note["markers"] = json.loads(note["markers"])
        return _iso(note, "updated_at")

    def list_notes(self, candidate_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT session_id, candidate_id, author_id, author_name, body, markers, updated_at "
                "FROM interview_note WHERE candidate_id = %s ORDER BY updated_at", (candidate_id,)).fetchall()
        out = []
        for row in rows:
            note = dict(row)
            if isinstance(note["markers"], str):
                note["markers"] = json.loads(note["markers"])
            out.append(_iso(note, "updated_at"))
        return out

    def begin_copilot(self, interview_id: str, *, max_calls: int, per_minute: int, now_ts: float) -> tuple[str | None, int]:
        with self._conn() as c, c.transaction():
            row = c.execute("SELECT copilot_calls, copilot_times FROM interview_session WHERE id = %s FOR UPDATE", (interview_id,)).fetchone()
            if not row:
                return "missing", 0
            times = row["copilot_times"]
            if isinstance(times, str):
                times = json.loads(times)
            times = [float(t) for t in (times or []) if now_ts - float(t) < 60]
            used = int(row["copilot_calls"] or 0)
            if used >= max_calls:
                c.execute("UPDATE interview_session SET copilot_times = %s::jsonb WHERE id = %s", (json.dumps(times), interview_id))
                return "calls", 0
            if len(times) >= per_minute:
                c.execute("UPDATE interview_session SET copilot_times = %s::jsonb WHERE id = %s", (json.dumps(times), interview_id))
                return "minute", max_calls - used
            times.append(now_ts)
            c.execute("UPDATE interview_session SET copilot_calls = %s, copilot_times = %s::jsonb WHERE id = %s",
                      (used + 1, json.dumps(times), interview_id))
            return None, max_calls - (used + 1)

    def copilot_remaining(self, interview_id: str, max_calls: int) -> int:
        with self._conn() as c:
            row = c.execute("SELECT copilot_calls FROM interview_session WHERE id = %s", (interview_id,)).fetchone()
        if not row:
            return 0
        return max(0, max_calls - int(row["copilot_calls"] or 0))

    def try_spend_tokens(self, day: str, amount: int, budget: int) -> bool:
        with self._conn() as c, c.transaction():
            row = c.execute("SELECT tokens FROM llm_budget WHERE day = %s FOR UPDATE", (day,)).fetchone()
            used = int(row["tokens"]) if row else 0
            if amount > 0 and used + amount > budget:
                return False
            updated = max(0, used + amount)
            c.execute(
                "INSERT INTO llm_budget (day, tokens) VALUES (%s, %s) ON CONFLICT (day) DO UPDATE SET tokens = EXCLUDED.tokens",
                (day, updated))
        return True

    def claim_eval_run(self, interview_id: str, limit: int) -> bool:
        with self._conn() as c, c.transaction():
            row = c.execute("SELECT eval_runs FROM interview_session WHERE id = %s FOR UPDATE", (interview_id,)).fetchone()
            if not row or int(row["eval_runs"] or 0) >= limit:
                return False
            c.execute("UPDATE interview_session SET eval_runs = eval_runs + 1 WHERE id = %s", (interview_id,))
        return True


def _remove_file(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        return
