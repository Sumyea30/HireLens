"""Job assistant endpoints (Graph 0): chat, state, and the human approval decision."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph.types import Command

from app.api.access import require_job
from app.api.audit import audit
from app.api.schemas import ApprovalIn, AssistantIn
from app.core.auth import require_roles
from app.graphs import initial_job_post_state
from app.graphs.job_post_graph import trim_history
from app.services.memory_store import EMPTY_PROFILE

log = logging.getLogger("hirelens.api")
router = APIRouter()
staff = require_roles("interviewer", "admin")
DONE = {"open", "rejected", "failed"}


def _config(job_id: str) -> dict:
    return {"configurable": {"thread_id": job_id}}


def _snapshot(req: Request, job_id: str):
    snap = req.app.state.services.job_graph.get_state(_config(job_id))
    if not snap.values:
        raise HTTPException(404, "Not found")
    return snap


def _paused(snap) -> tuple[str | None, dict | None]:
    node = snap.next[0] if snap.next else None
    value = None
    for task in snap.tasks:
        if task.interrupts:
            value = task.interrupts[0].value
            break
    return node, value


def build_reply(req: Request, job_id: str) -> dict:
    """Map the graph state to the AssistantReply shape the frontend expects."""
    svc = req.app.state.services
    snap = _snapshot(req, job_id)
    v = snap.values
    node, payload = _paused(snap)
    messages = trim_history(list(v.get("conversation", [])))
    approval = None
    if node == "await_user_reply":
        messages.append({"role": "assistant", "content": v.get("pending_question", "")})
    elif node == "human_approval" and payload:
        approval = {k: payload.get(k, []) for k in (
            "draft_post", "proposed_channels", "policy_evidence", "compliance_findings",
            "mandatory_failures", "unresolved_items")}
        approval["draft_post"] = payload.get("draft_post", "")
        messages.append({"role": "assistant", "content": "I drafted a job post using your company policies. Please review it and choose where to post it."})
    graph_status = v.get("status", "collecting")
    if graph_status == "open":
        messages.append({"role": "assistant", "content": "Published. Candidates can now apply by email."})
    elif graph_status == "rejected":
        messages.append({"role": "assistant", "content": "Understood. The draft was rejected and nothing was published."})
    elif graph_status == "failed":
        failures = [f"{r['channel']}: {r.get('error') or 'failed'}" for r in v.get("publish_results", []) if r["status"] != "ok"]
        reason = f" ({'; '.join(failures)})" if failures else ""     # short safe codes only, never tool output
        messages.append({"role": "assistant", "content": f"The post could not be published{reason}. Nothing is live. Check the channel settings, then start a new job." if failures
                         else "Something went wrong while preparing the post. Nothing was published. Submit the form again to try once more."})
    status = {"awaiting_approval": "awaiting_approval", "open": "open", "rejected": "rejected", "failed": "failed"}.get(graph_status, "collecting")
    return {
        "job_id": job_id, "status": status, "messages": messages,
        "profile": {k: val for k, val in v.get("profile", {}).items() if k in EMPTY_PROFILE},
        "missing_fields": v.get("missing_fields", []), "approval": approval,
        "publish_results": [{"channel": r["channel"], "status": r["status"], "url": r.get("url"), "error": r.get("error")}
                            for r in v.get("publish_results", [])],
        "apply_email": svc.settings.apply_email if graph_status == "open" else None,
    }


def _sync_job(req: Request, job_id: str, reply: dict) -> None:
    store = req.app.state.services.store
    store.update_job(job_id, title=reply["profile"].get("title") or None,
                     status={"collecting": "draft"}.get(reply["status"], reply["status"]),
                     profile=reply["profile"])


def _can_retry_draft(snap) -> bool:
    """The profile is complete, drafting stopped before a post existed, and the graph is not paused."""
    values = snap.values or {}
    return not snap.next and values.get("status") == "failed" and values.get("profile_complete") and not values.get("draft_post")


def _run(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except Exception:
        log.error("graph_run_failed", exc_info=False)   # type only, no content
        raise HTTPException(502, "The assistant could not complete that step. Please try again.") from None


@router.post("/jobs/assistant")
def assistant_message(body: AssistantIn, req: Request, user: dict = Depends(staff)):
    svc = req.app.state.services
    message = body.message.strip()
    if not message:
        raise HTTPException(422, "Message is empty")
    template = None if body.template is None else body.template.strip()
    if body.job_id is None:
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        svc.store.create_job(job_id, user["sub"])
        audit(req, user["role"], "job_created", "job", job_id)
        _run(lambda: svc.job_graph.invoke(
            initial_job_post_state(job_id=job_id, first_message=message, description_template=template or ""),
            _config(job_id)))
    else:
        job_id = body.job_id
        require_job(svc.store, user, job_id)
        snap = _snapshot(req, job_id)
        node, _ = _paused(snap)
        if node == "await_user_reply":
            if template is not None:
                svc.job_graph.update_state(_config(job_id), {"description_template": template})
            _run(lambda: svc.job_graph.invoke(Command(resume=message), _config(job_id)))
        elif _can_retry_draft(snap):
            if template is not None:
                svc.job_graph.update_state(_config(job_id), {"description_template": template})
            svc.job_graph.update_state(_config(job_id), {"status": "drafting"}, as_node="retrieve_policy")
            _run(lambda: svc.job_graph.invoke(None, _config(job_id)))
        else:
            raise HTTPException(409, "This job is not waiting for a message.")
    log.info("assistant_message", extra={"job_id": job_id, "role": user["role"]})
    reply = build_reply(req, job_id)
    _sync_job(req, job_id, reply)
    return reply


@router.get("/jobs/{job_id}/assistant")
def assistant_state(job_id: str, req: Request, user: dict = Depends(staff)):
    require_job(req.app.state.services.store, user, job_id)
    return build_reply(req, job_id)


@router.post("/jobs/{job_id}/approval")
def approval(job_id: str, body: ApprovalIn, req: Request, user: dict = Depends(staff)):
    svc = req.app.state.services
    require_job(svc.store, user, job_id)
    node, _ = _paused(_snapshot(req, job_id))
    if node != "human_approval":
        raise HTTPException(409, "This job is not waiting for approval.")
    decision = {"action": body.action, "channels": body.channels, "edited_post": body.edited_post,
                "feedback": body.feedback, "approver_role": user["role"]}
    _run(lambda: svc.job_graph.invoke(Command(resume=decision), _config(job_id)))
    log.info("approval_decision", extra={"job_id": job_id, "action": body.action, "role": user["role"]})
    reply = build_reply(req, job_id)
    _sync_job(req, job_id, reply)
    expected = {"approve": "open", "reject": "rejected"}.get(body.action)
    action = {"approve": "post_approved", "reject": "post_rejected", "changes": "post_changes_requested"}[body.action]
    audit(req, user["role"], action, "job", job_id, "ok" if expected is None or reply["status"] == expected else "error")
    return reply
