"""CV collection endpoints (F2): upload, list, view, and delete a candidate with all their data."""
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from app.api.access import require_job
from app.api.audit import audit
from app.core.auth import require_roles
from app.services.review import build_review
from app.services.screening import screen_many

router = APIRouter()
staff = require_roles("interviewer", "admin")
MAX_FILES_PER_REQUEST = 20


def _view(c: dict) -> dict:
    # The display name never comes from the file name. CV parsing fills the real name later.
    return {"id": c["id"], "job_id": c["job_id"], "name": c["name"] or f"Applicant {c['id'][-4:].upper()}",
            "status": c["status"], "aggregate_score": c["aggregate_score"], "source": c["source"],
            "received_at": c["received_at"], "assessment_id": c["assessment_id"]}


def _empty_parsed() -> dict:
    return {"skills": [], "education": [], "work_history": [], "certifications": [], "projects": [],
            "total_experience_years": None}


def _parsed(raw: dict | None) -> dict:
    raw = raw or {}
    out = _empty_parsed()
    for key in ("skills", "education", "work_history", "certifications", "projects"):
        out[key] = list(raw.get(key) or [])
    out["total_experience_years"] = raw.get("total_experience_years")
    return out


def _snippet(store, ref: str) -> str:
    kind, _, rest = ref.partition(":")
    if kind != "cv":
        return ""
    candidate_id, sep, chunk_id = rest.rpartition(":")
    if not sep:
        return ""
    item = store.get_item(f"cv:{candidate_id}", chunk_id)
    return item["text"] if item else ""


def _screening(store, c: dict) -> dict:
    """Skills, scores and requirement matches written by Graph A. Empty until screening finishes."""
    result = store.load_cv_result(c["id"], c["job_id"])
    if not result or result.get("status") != "completed":
        return {"parsed": _empty_parsed(), "dimension_scores": [], "matches": []}
    job = store.get_job(c["job_id"]) or {}
    texts = {r["id"]: r["text"] for r in job.get("requirements") or []}
    weights = result.get("weights") or {}
    matches = []
    for m in result.get("matches") or []:
        refs = list(m.get("evidence_refs") or [])
        matches.append({
            "requirement_id": m["requirement_id"],
            "requirement_text": texts.get(m["requirement_id"], ""),
            "status": m["status"],
            "justification": m["justification"],
            "evidence": [{"ref": ref, "text": _snippet(store, ref)} for ref in refs],
        })
    scores = []
    for s in result.get("dimension_scores") or []:
        scores.append({
            "dimension": s["dimension"],
            "score": float(s["score"]),
            "weight": float(s.get("weight", weights.get(s["dimension"], 0)) or 0),
            "justification": s["justification"],
            "evidence_refs": list(s.get("evidence_refs") or []),
        })
    return {"parsed": _parsed(result.get("parsed_cv")), "dimension_scores": scores, "matches": matches}


@router.post("/jobs/{job_id}/cvs")
def upload_cvs(job_id: str, files: list[UploadFile], req: Request, user: dict = Depends(staff)):
    svc = req.app.state.services
    if not files or len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(422, f"Send between 1 and {MAX_FILES_PER_REQUEST} files.")
    require_job(svc.store, user, job_id)
    results = []
    for f in files:
        data = f.file.read(svc.intake.max_bytes + 1)          # one byte over the limit is enough to reject
        res = svc.intake.accept(job_id=job_id, filename=f.filename or "", data=data, source="upload", actor_role=user["role"])
        results.append({"filename": res["filename"], "status": res["status"], "reason": res["reason"]})
    return results


@router.post("/jobs/{job_id}/cv-batch")
def batch_cvs(job_id: str, files: list[UploadFile], req: Request, user: dict = Depends(staff)):
    """Upload several CVs and score them now. The same file against the same job keeps its score."""
    svc = req.app.state.services
    if not files or len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(422, f"Send between 1 and {MAX_FILES_PER_REQUEST} files.")
    require_job(svc.store, user, job_id)
    uploaded = []
    for f in files:
        data = f.file.read(svc.intake.max_bytes + 1)
        res = svc.intake.accept(job_id=job_id, filename=f.filename or "", data=data, source="upload", actor_role=user["role"])
        uploaded.append({"filename": res["filename"], "status": res["status"], "reason": res["reason"],
                         "candidate_id": res.get("candidate_id"), "aggregate_score": None})
    outcomes = screen_many(svc, [item["candidate_id"] for item in uploaded if item["status"] == "queued" and item["candidate_id"]])
    for item in uploaded:
        outcome = outcomes.get(item["candidate_id"])
        if outcome is None:
            continue
        cand = svc.store.get_candidate(item["candidate_id"]) or {}
        if outcome == "completed":
            item["status"] = "screened"
            item["aggregate_score"] = cand.get("aggregate_score")
        else:
            item["status"] = "failed"
            item["reason"] = cand.get("failure_reason") or "Screening could not be completed."
    return {"files": uploaded, "review": build_review(svc.store, job_id)}


@router.get("/jobs/{job_id}/cv-review")
def cv_review(job_id: str, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    require_job(store, user, job_id)
    return build_review(store, job_id)


@router.get("/candidates")
def list_candidates(req: Request, job_id: str | None = None, user: dict = Depends(staff)):
    store = req.app.state.services.store
    if job_id:
        require_job(store, user, job_id)
    rows = store.list_candidates(job_id)
    if user["role"] != "admin" and not job_id:
        mine = {j["id"] for j in store.list_jobs() if j.get("owner_id") == user["sub"]}
        rows = [c for c in rows if c["job_id"] in mine]
    return [_view(c) for c in rows]


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    c = store.get_candidate(candidate_id)
    if not c:
        raise HTTPException(404, "Not found")
    require_job(store, user, c["job_id"])
    report = store.load_assessment_for_candidate(candidate_id) if hasattr(store, "load_assessment_for_candidate") else None
    evaluation = None
    if report:
        from app.services.evaluate import public_assessment
        evaluation = public_assessment(store, report)
    return {**_view(c), "email": c.get("email"), "phone": c.get("phone"), "failure_reason": c.get("failure_reason"),
            **_screening(store, c), "notes": store.list_notes(candidate_id), "evaluation": evaluation}


@router.delete("/admin/candidates/{candidate_id}", status_code=204)
def delete_candidate(candidate_id: str, req: Request, user: dict = Depends(require_roles("admin"))):
    if not req.app.state.services.intake.delete_candidate(candidate_id, user["role"]):
        raise HTTPException(404, "Not found")
