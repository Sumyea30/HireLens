"""Role-gated views of candidate identity fields (name, photo)."""


def display_name(c: dict | None, *, reveal: bool = False) -> str:
    c = c or {}
    if reveal and c.get("name"):
        return c["name"]
    cid = c.get("id") or ""
    return f"Applicant {cid[-4:].upper()}" if cid else "Applicant"


def public_candidate(user: dict, c: dict, *, room: bool = False) -> dict:
    """Strip identity for interviewers. Admins see name/photo; room staff see name only."""
    role = (user or {}).get("role")
    is_admin = role == "admin"
    reveal_name = is_admin or room
    out = {
        "id": c["id"],
        "job_id": c["job_id"],
        "name": display_name(c, reveal=reveal_name),
        "status": c["status"],
        "aggregate_score": c.get("aggregate_score"),
        "source": c.get("source"),
        "received_at": c.get("received_at"),
        "assessment_id": c.get("assessment_id"),
    }
    if is_admin or room:
        # Email stays available to staff for scheduling; name only for admin/room.
        pass
    if is_admin:
        out["real_name"] = c.get("name")
        out["has_photo"] = bool(c.get("photo_path"))
        out["photo_url"] = f"/admin/candidates/{c['id']}/photo" if c.get("photo_path") else None
    else:
        out["has_photo"] = False
        out["photo_url"] = None
    return out


def attach_contact(user: dict, view: dict, c: dict) -> dict:
    """Add email/phone for staff detail views (needed to schedule interviews). Never add photo for non-admin."""
    view = {**view, "email": c.get("email"), "phone": c.get("phone"), "failure_reason": c.get("failure_reason")}
    if user.get("role") != "admin":
        view.pop("real_name", None)
    return view
