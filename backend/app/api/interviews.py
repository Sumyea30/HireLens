"""Schedule an interview and email both people. Staff-only tools sit beside the room."""
import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.access import require_job
from app.api.audit import audit
from app.api.schemas import InterviewIn, JoinIn
from app.core.auth import current_user, issue_token, require_roles
from app.services.copilot import LimitReached, suggest
from app.services.evaluate import NotReady, public_assessment, run_evaluation
from app.services.invite import INVITE_MINUTES, invite_parts, send_invite, smtp_account
from app.services.transcript import seal_transcript

log = logging.getLogger("hirelens.api")
router = APIRouter()
staff = require_roles("interviewer", "admin")
READY = {"screened", "interview_scheduled", "interviewed"}
NOTE_LIMIT = 5000


def _auto_evaluate(services, interview: dict, actor_role: str) -> None:
    """Best-effort Gemini rating after End. Failures never undo interview completion."""
    interview_id = interview["id"]
    try:
        run_evaluation(services, interview)
        services.store.add_audit(actor_role, "interview_evaluated", "interview", interview_id, "ok")
    except NotReady as exc:
        services.store.add_audit(actor_role, "interview_evaluated", "interview", interview_id, "skipped")
        log.info("auto_eval_skipped", extra={"interview_id": interview_id, "reason": str(exc.message)})
    except LimitReached as exc:
        services.store.add_audit(actor_role, "interview_evaluated", "interview", interview_id, "limit")
        log.info("auto_eval_limit", extra={"interview_id": interview_id, "reason": str(exc.message)})
    except Exception:
        services.store.add_audit(actor_role, "interview_evaluated", "interview", interview_id, "error")
        log.exception("auto_eval_failed", extra={"interview_id": interview_id})


class RescheduleIn(BaseModel):
    starts_at: datetime


class CopilotIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class NoteIn(BaseModel):
    body: str = Field(max_length=NOTE_LIMIT)
    mark: bool = False


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _public(row: dict, token: str = "") -> dict:
    return {
        "id": row["id"], "job_id": row["job_id"], "job_title": row.get("job_title") or "",
        "candidate_id": row["candidate_id"], "candidate_name": row.get("candidate_name") or "Candidate",
        "status": row["status"], "created_at": row.get("created_at"), "scheduled_at": row.get("scheduled_at"),
        "started_at": row.get("started_at"), "ended_at": row.get("ended_at"), "join_token": token,
    }


def _visible(store, user) -> set[str]:
    jobs = store.list_jobs()
    if user["role"] == "admin":
        return {j["id"] for j in jobs}
    return {j["id"] for j in jobs if j.get("owner_id") == user["sub"]}


def _staff_interview(store, user, interview_id: str) -> dict:
    row = store.get_interview(interview_id)
    if not row or row["job_id"] not in _visible(store, user):
        raise HTTPException(404, "Not found")
    return row


def _when(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(422, "Choose a date and time.")
    if value < datetime.now(timezone.utc) - timedelta(minutes=5):
        raise HTTPException(422, "Choose a time that has not already passed.")
    return value


def _interviewer_email(store, settings, user) -> str:
    recruiter = store.get_recruiter(user["sub"]) if hasattr(store, "get_recruiter") else None
    if recruiter and recruiter.get("email"):
        return str(recruiter["email"]).strip().lower()
    configured = (settings.users.get(user["sub"]) or {}).get("email") or ""
    return str(configured).strip().lower()


def _organizer(settings) -> str:
    _host, _port, user, _password = smtp_account(settings)
    return user or "hiring@example.com"


def _deliver(settings, to: str, parts) -> tuple[bool, str | None]:
    try:
        send_invite(settings, to, parts.subject, parts.plain, html=parts.html, ics=parts.ics)
        return True, None
    except Exception as exc:
        reason = str(exc)
        text = ("Email is not configured, so the invitation was not sent." if reason == "not_configured"
                else "The invitation email could not be sent. Copy the interview link and send it yourself.")
        return False, text


def _send_both(req, row, starts: datetime, candidate_url: str, candidate_email: str, interviewer_email: str) -> dict:
    settings = req.app.state.services.settings
    base = settings.public_base_url
    organizer = _organizer(settings)
    uid = row.get("calendar_uid") or f"{row['id']}@hirelens"
    sequence = int(row.get("calendar_sequence") or 0)
    interviewer_url = f"{base}/interviews/{row['id']}/room"
    others = [interviewer_email] if interviewer_email else []
    candidate_parts = invite_parts(
        job_title=row.get("job_title") or "the role", starts_at=starts, room_url=candidate_url,
        uid=uid, sequence=sequence, organizer=organizer, attendee=candidate_email, extra_attendees=others,
    )
    sent, error = _deliver(settings, candidate_email, candidate_parts)
    interviewer_sent, interviewer_error = False, None
    if interviewer_email:
        staff_parts = invite_parts(
            job_title=row.get("job_title") or "the role", starts_at=starts, room_url=interviewer_url,
            uid=uid, sequence=sequence, organizer=organizer, attendee=interviewer_email,
            extra_attendees=[candidate_email],
        )
        interviewer_sent, interviewer_error = _deliver(settings, interviewer_email, staff_parts)
    else:
        interviewer_error = "This account has no email address. Copy the interviewer link."
    if not sent or (interviewer_email and not interviewer_sent):
        log.info("invite_not_sent", extra={"interview_id": row["id"], "candidate_sent": sent, "interviewer_sent": interviewer_sent})
    return {
        "email_sent": sent, "email_error": error,
        "interviewer_email_sent": interviewer_sent, "interviewer_email_error": interviewer_error,
        "interviewer_room": interviewer_url,
    }


def _elapsed_ms(started_at: str | None) -> int:
    if not started_at:
        return 0
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))


@router.get("/interviews")
def list_interviews(req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    allowed = _visible(store, user)
    return [_public(row) for row in store.list_interviews() if row["job_id"] in allowed]


@router.get("/interviews/{interview_id}")
def get_interview(interview_id: str, req: Request, user: dict = Depends(current_user)):
    store = req.app.state.services.store
    row = store.get_interview(interview_id)
    if not row:
        raise HTTPException(404, "Not found")
    if user["role"] == "candidate":
        if user["sub"] != row["candidate_id"]:
            raise HTTPException(404, "Not found")
        return _public(row)
    if row["job_id"] not in _visible(store, user):
        raise HTTPException(404, "Not found")
    return _public(row)


@router.post("/interviews")
def create_interview(body: InterviewIn, req: Request, user: dict = Depends(staff)):
    starts = _when(body.starts_at)
    store = req.app.state.services.store
    job = require_job(store, user, body.job_id)
    cand = store.get_candidate(body.candidate_id)
    if not cand or cand["job_id"] != job["id"] or cand["status"] not in READY:
        raise HTTPException(404, "Not found")
    email = (cand.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(422, "This candidate has no email address. Invitations go to the address they applied from.")
    token = secrets.token_urlsafe(24)
    interview_id = f"int-{uuid.uuid4().hex[:8]}"
    row = store.create_interview({
        "id": interview_id, "job_id": job["id"], "candidate_id": cand["id"],
        "join_token_hash": _hash(token), "scheduled_at": starts.isoformat(),
        "job_title": job.get("title") or "the role", "candidate_name": cand.get("name") or "Candidate",
        "calendar_uid": f"{interview_id}@hirelens", "calendar_sequence": 0,
    })
    settings = req.app.state.services.settings
    room_url = f"{settings.public_base_url}/join/{token}"
    mailed = _send_both(req, row, starts, room_url, email, _interviewer_email(store, settings, user))
    audit(req, user["role"], "interview_created", "interview", interview_id, "ok" if mailed["email_sent"] else "error")
    return {**_public(row, token), **mailed, "duration_minutes": INVITE_MINUTES}


@router.post("/interviews/{interview_id}/reschedule")
def reschedule_interview(interview_id: str, body: RescheduleIn, req: Request, user: dict = Depends(staff)):
    starts = _when(body.starts_at)
    store = req.app.state.services.store
    current = _staff_interview(store, user, interview_id)
    cand = store.get_candidate(current["candidate_id"]) or {}
    email = (cand.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(422, "This candidate has no email address. Invitations go to the address they applied from.")
    token = secrets.token_urlsafe(24)
    row = store.reschedule_interview(interview_id, starts.isoformat(), _hash(token))
    if not row:
        raise HTTPException(404, "Not found")
    settings = req.app.state.services.settings
    mailed = _send_both(req, row, starts, f"{settings.public_base_url}/join/{token}", email, _interviewer_email(store, settings, user))
    audit(req, user["role"], "interview_rescheduled", "interview", interview_id, "ok" if mailed["email_sent"] else "error")
    return {**_public(row, token), **mailed, "duration_minutes": INVITE_MINUTES}


@router.post("/interviews/{interview_id}/end")
async def end_interview(
    interview_id: str,
    req: Request,
    background: BackgroundTasks,
    user: dict = Depends(staff),
):
    services = req.app.state.services
    store = services.store
    row = _staff_interview(store, user, interview_id)
    done = store.end_interview(interview_id)
    seal_transcript(store, services.settings, interview_id)
    hub = getattr(req.app.state, "rooms", None)
    if hub:
        await hub.broadcast(interview_id, {"type": "ended"})
    audit(req, user["role"], "interview_ended", "interview", interview_id)
    finished = done or row
    background.add_task(_auto_evaluate, services, finished, user["role"])
    return _public(finished)


@router.get("/interviews/{interview_id}/transcript")
def get_transcript(interview_id: str, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    _staff_interview(store, user, interview_id)
    doc = store.transcript_document(interview_id)
    if not doc:
        raise HTTPException(404, "Not found")
    return doc


@router.get("/interviews/{interview_id}/brief")
def interview_brief(interview_id: str, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    row = _staff_interview(store, user, interview_id)
    cand = store.get_candidate(row["candidate_id"]) or {}
    screening = _screening(store, cand) if cand else {"dimension_scores": [], "matches": []}
    gaps = [m for m in screening["matches"] if m["status"] != "met"]
    return {
        "name": cand.get("name") or row.get("candidate_name") or "Candidate",
        "role": row.get("job_title") or "",
        "aggregate_score": cand.get("aggregate_score"),
        "dimension_scores": screening["dimension_scores"],
        "matches": screening["matches"],
        "gaps": gaps,
    }


@router.get("/interviews/{interview_id}/copilot")
def copilot_quota(interview_id: str, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    settings = req.app.state.services.settings
    _staff_interview(store, user, interview_id)
    remaining = store.copilot_remaining(interview_id, settings.copilot_max_calls)
    return {"remaining": remaining, "limit": settings.copilot_max_calls}


@router.post("/interviews/{interview_id}/copilot")
def ask_copilot(interview_id: str, body: CopilotIn, req: Request, user: dict = Depends(staff)):
    services = req.app.state.services
    row = _staff_interview(services.store, user, interview_id)
    try:
        return suggest(services.store, services.deps.llm, services.settings, row, body.message.strip())
    except LimitReached as exc:
        return JSONResponse({"detail": exc.message}, status_code=429)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/interviews/{interview_id}/notes")
def get_notes(interview_id: str, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    _staff_interview(store, user, interview_id)
    return store.get_note(interview_id) or {"body": "", "markers": [], "author_name": "", "updated_at": None}


@router.put("/interviews/{interview_id}/notes")
def save_notes(interview_id: str, body: NoteIn, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    row = _staff_interview(store, user, interview_id)
    existing = store.get_note(interview_id) or {"markers": []}
    markers = list(existing.get("markers") or [])
    if body.mark:
        store.ensure_started(interview_id)
        fresh = store.get_interview(interview_id) or row
        markers.append({"offset_ms": _elapsed_ms(fresh.get("started_at")), "at": datetime.now(timezone.utc).isoformat()})
    note = store.save_note(interview_id, {
        "session_id": interview_id, "candidate_id": row["candidate_id"], "author_id": user["sub"],
        "author_name": user.get("name") or "Interviewer",
        "body": body.body, "markers": markers, "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    audit(req, user["role"], "interview_note_saved", "interview", interview_id)
    return note


@router.post("/interviews/{interview_id}/evaluate")
@router.post("/interviews/{interview_id}/assessment")
def evaluate_interview(interview_id: str, req: Request, user: dict = Depends(staff)):
    services = req.app.state.services
    row = _staff_interview(services.store, user, interview_id)
    try:
        report = run_evaluation(services, row)
    except LimitReached as exc:
        return JSONResponse({"detail": exc.message}, status_code=429)
    except NotReady as exc:
        raise HTTPException(422, exc.message) from None
    audit(req, user["role"], "interview_evaluated", "interview", interview_id)
    return report


@router.get("/assessments")
def list_assessments(req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    allowed = _visible(store, user)
    rows = []
    for report in store.list_assessments():
        if report.get("job_id") not in allowed:
            continue
        view = public_assessment(store, report)
        rows.append({k: view[k] for k in (
            "id", "candidate_id", "candidate_name", "job_id", "job_title", "overall_position", "combined", "status", "created_at")})
    return rows


@router.get("/assessments/{assessment_id}")
def get_assessment(assessment_id: str, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    report = store.load_assessment(assessment_id)
    if not report or report.get("job_id") not in _visible(store, user):
        raise HTTPException(404, "Not found")
    return public_assessment(store, report)


@router.post("/join/{token}")
def join_interview(token: str, body: JoinIn, req: Request):
    if not body.consent:
        raise HTTPException(400, "Please accept the notice to join the interview.")
    store = req.app.state.services.store
    row = store.get_interview_by_token_hash(_hash(token))
    if not row or row["status"] == "completed":
        raise HTTPException(404, "This interview link is not valid.")
    store.mark_interview_consent(row["id"])
    settings = req.app.state.services.settings
    issued = issue_token(settings.auth_secret, row["candidate_id"], row.get("candidate_name") or "Candidate", "candidate", settings.token_ttl_minutes)
    return {"token": issued, "user": {"id": row["candidate_id"], "name": row.get("candidate_name") or "Candidate", "role": "candidate"},
            "interview_id": row["id"]}


def _screening(store, cand: dict) -> dict:
    """Scores and requirement matches for the interviewer brief. Empty until screening finishes."""
    from app.api.candidates import _screening as screening
    return screening(store, cand)
