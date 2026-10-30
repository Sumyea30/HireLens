"""Funnel Doctor endpoints (F11): requirement funnel report and post-revision suggestion."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.api.access import require_job
from app.api.audit import audit
from app.api.assistant import _config, _paused, _run, _snapshot, _sync_job, build_reply
from app.core.auth import require_roles
from app.graphs.common import safe_structured
from app.services.funnel import aggregates_for_llm, funnel_report

log = logging.getLogger("hirelens.api")
router = APIRouter()
staff = require_roles("interviewer", "admin")

SUGGEST_ACTION = "funnel_suggest"
SUGGEST_SYSTEM = (
    "You revise a job post using only aggregate screening funnel numbers and the "
    "current post text. Suggest clearer or fairer wording for bottlenecks and "
    "non-discriminating preferred criteria. Never invent candidate names, CV text, "
    "transcripts, protected attributes, or personal data. Output JSON only."
)


class FunnelRevisionOut(BaseModel):
    feedback: str = Field(min_length=1, description="Reviewer-style feedback for a redraft")
    summary: str = Field(min_length=1, description="Short summary for the recruiter UI")


def _utc_day_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _feed_changes_draft(req: Request, job_id: str, feedback: str, user: dict) -> dict:
    """Inject feedback as a changes-requested redraft so human approval still applies."""
    svc = req.app.state.services
    try:
        snap = _snapshot(req, job_id)
    except HTTPException:
        raise HTTPException(409, "This job has no assistant draft to revise.") from None
    node, _ = _paused(snap)
    decision = {
        "action": "changes",
        "channels": [],
        "edited_post": None,
        "feedback": feedback,
        "approver_role": user["role"],
    }
    if node == "human_approval":
        _run(lambda: svc.job_graph.invoke(Command(resume=decision), _config(job_id)))
    elif snap.values:
        svc.job_graph.update_state(
            _config(job_id),
            {
                "decision": decision,
                "retry_count": 0,
                "compliance_findings": [],
                "mandatory_failures": [],
                "status": "drafting",
            },
            as_node="reset_for_redraft",
        )
        _run(lambda: svc.job_graph.invoke(None, _config(job_id)))
    else:
        raise HTTPException(409, "This job has no assistant draft to revise.")
    reply = build_reply(req, job_id)
    _sync_job(req, job_id, reply)
    return reply


@router.get("/jobs/{job_id}/funnel")
def get_funnel(job_id: str, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    require_job(store, user, job_id)
    report = funnel_report(store, job_id)
    if report is None:
        raise HTTPException(404, "Not found")
    audit(req, user["role"], "funnel_viewed", "job", job_id)
    return report


@router.post("/jobs/{job_id}/funnel/suggest-revision")
def suggest_revision(job_id: str, req: Request, user: dict = Depends(staff)):
    svc = req.app.state.services
    store = svc.store
    job = require_job(store, user, job_id)
    report = funnel_report(store, job_id)
    if report is None:
        raise HTTPException(404, "Not found")

    limit = svc.settings.funnel_suggest_max_per_day
    used = store.count_audit_since(SUGGEST_ACTION, job_id, _utc_day_start())
    if used >= limit:
        audit(req, user["role"], SUGGEST_ACTION, "job", job_id, "denied")
        raise HTTPException(429, f"At most {limit} revision suggestions per job per day.")

    payload = aggregates_for_llm(report, job.get("post_text") or "")
    user_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    out, errs = safe_structured(
        svc.deps.llm, "funnel_suggest_revision", FunnelRevisionOut,
        system=SUGGEST_SYSTEM, user=user_json, prompt_version="funnel-rev-v1",
    )
    if out is None:
        audit(req, user["role"], SUGGEST_ACTION, "job", job_id, "error")
        code = (errs or [{}])[0].get("code", "LLMError") if errs else "LLMError"
        log.error("funnel_suggest_failed", extra={"job_id": job_id, "error_type": code})
        raise HTTPException(502, "The revision suggestion could not be completed. Please try again.") from None

    audit(req, user["role"], SUGGEST_ACTION, "job", job_id, "ok")
    try:
        reply = _feed_changes_draft(req, job_id, out.feedback, user)
    except HTTPException as exc:
        if exc.status_code == 409:
            return {
                "summary": out.summary,
                "feedback": out.feedback,
                "assistant": None,
                "fed_to_assistant": False,
            }
        raise
    audit(req, user["role"], "funnel_suggest_fed", "job", job_id, "ok")
    return {
        "summary": out.summary,
        "feedback": out.feedback,
        "assistant": reply,
        "fed_to_assistant": True,
    }
