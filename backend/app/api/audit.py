import hashlib
import logging

from fastapi import Request

log = logging.getLogger("hirelens.audit")


def short_hash(value: str) -> str:
    """Identify a user in the audit log without storing their username."""
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def audit(req: Request, actor_role: str, action: str, entity: str, entity_id: str, outcome: str = "ok") -> None:
    """Best effort: an audit failure must never break the request. IDs and outcomes only, never personal data."""
    try:
        req.app.state.services.store.add_audit(actor_role, action, entity, entity_id, outcome)
    except Exception as exc:
        log.error("audit_failed", extra={"error_type": type(exc).__name__})
