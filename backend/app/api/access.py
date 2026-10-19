"""Who may see a job. Admins see every job. A recruiter sees only the jobs they posted."""
from fastapi import HTTPException


def can_see_job(user: dict, job: dict | None) -> bool:
    if not job:
        return False
    if user.get("role") == "admin":
        return True
    return job.get("owner_id") == user.get("sub")


def require_job(store, user: dict, job_id: str) -> dict:
    job = store.get_job(job_id)
    if not can_see_job(user, job):
        raise HTTPException(404, "Not found")
    return job
