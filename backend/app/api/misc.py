"""Auth, health, jobs list, policies and evidence endpoints."""
import time
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile

from app.api.access import require_job
from app.api.audit import audit, short_hash
from app.api import clientlog
from app.api.schemas import ClientLogIn, LoginIn, SignupIn, TemplateIn, WeightsIn
from app.core.auth import current_user, hash_password, issue_token, require_roles, valid_email, verify_password
from app.services.memory_store import DuplicateRecruiter
from app.services.policy import MAX_BYTES, TEMPLATE_TITLE, PolicyError

router = APIRouter()
staff = require_roles("interviewer", "admin")
_failures: dict[str, list[float]] = defaultdict(list)   # simple login throttle (per process)


@router.get("/health")
def health():
    return {"status": "ok"}


def _too_many(key: str) -> bool:
    now = time.time()
    _failures[key] = [t for t in _failures[key] if now - t < 60]
    if len(_failures[key]) >= 5:
        return True
    return False


def _deny(key: str) -> None:
    _failures[key].append(time.time())


def _session(req: Request, user_id: str, name: str, role: str) -> dict:
    s = req.app.state.services.settings
    token = issue_token(s.auth_secret, user_id, name, role, s.token_ttl_minutes)
    return {"token": token, "user": {"id": user_id, "name": name, "role": role}}


@router.post("/auth/signup")
def signup(body: SignupIn, req: Request):
    email = body.email.strip().lower()
    name = body.name.strip()
    if not name or not valid_email(email):
        raise HTTPException(422, "Enter your name and a valid email address.")
    if _too_many(email):
        raise HTTPException(429, "Too many attempts")
    try:
        account = req.app.state.services.store.create_recruiter({
            "id": f"rec-{uuid.uuid4().hex[:8]}", "email": email, "name": name,
            "password_hash": hash_password(body.password), "role": "interviewer",
        })
    except DuplicateRecruiter:
        _deny(email)
        audit(req, "system", "signup_failed", "user", short_hash(email), "denied")
        raise HTTPException(409, "An account with this email already exists.") from None
    audit(req, account["role"], "signup", "user", short_hash(email))
    return _session(req, account["id"], account["name"], account["role"])


@router.post("/auth/login")
def login(body: LoginIn, req: Request):
    s = req.app.state.services.settings
    key = body.username.strip().lower()
    if _too_many(key):
        raise HTTPException(429, "Too many attempts")
    account = req.app.state.services.store.get_recruiter_by_email(key)
    if account:
        if not verify_password(body.password, account["password_hash"]):
            _deny(key)
            audit(req, "system", "login_failed", "user", short_hash(key), "denied")
            raise HTTPException(401, "Invalid credentials")
        audit(req, account["role"], "login", "user", short_hash(key))
        return _session(req, account["id"], account["name"], account["role"])
    user = s.users.get(key)
    if not user or not verify_password(body.password, user.get("hash", "")):
        _deny(key)
        audit(req, "system", "login_failed", "user", short_hash(key), "denied")
        raise HTTPException(401, "Invalid credentials")   # same answer for unknown user and wrong password
    role = user.get("role", "interviewer")
    name = user.get("name", key)
    audit(req, role, "login", "user", short_hash(key))
    return _session(req, key, name, role)


@router.get("/jobs")
def list_jobs(req: Request, user: dict = Depends(staff)):
    jobs = req.app.state.services.store.list_jobs()
    if user["role"] == "admin":
        return jobs
    return [j for j in jobs if j.get("owner_id") == user["sub"]]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, req: Request, user: dict = Depends(staff)):
    return require_job(req.app.state.services.store, user, job_id)


@router.get("/policies")
def list_policies(req: Request, user: dict = Depends(staff)):
    return req.app.state.services.policy.list()


@router.get("/policies/template")
def get_template(req: Request, user: dict = Depends(staff)):
    policy = req.app.state.services.policy
    stored = policy.store.find_active_policy(TEMPLATE_TITLE) is not None
    return {"text": policy.template_text(), "stored": stored}


@router.put("/policies/template")
def save_template(body: TemplateIn, req: Request, user: dict = Depends(staff)):
    policy = req.app.state.services.policy
    try:
        saved = policy.ingest_text(TEMPLATE_TITLE, body.text)
    except PolicyError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:
        raise HTTPException(502, "The template could not be saved. Please try again.") from None
    audit(req, user["role"], "policy_uploaded", "policy", saved["id"])
    return {"text": policy.template_text(), "stored": True}


@router.post("/policies")
async def upload_policy(file: UploadFile, req: Request, user: dict = Depends(staff)):
    data = await file.read(MAX_BYTES + 1)
    try:
        policy = req.app.state.services.policy.ingest(file.filename or "policy.txt", data)
    except PolicyError as exc:
        raise HTTPException(422, str(exc)) from None
    audit(req, user["role"], "policy_uploaded", "policy", policy["id"])
    return policy


@router.delete("/policies/{policy_id}", status_code=204)
def delete_policy(policy_id: str, req: Request, user: dict = Depends(require_roles("admin"))):
    req.app.state.services.policy.delete(policy_id)
    audit(req, user["role"], "policy_deleted", "policy", policy_id)


@router.get("/evidence")
def evidence(ref: str, req: Request, user: dict = Depends(staff)):
    """Text behind an evidence reference. Policy refs work now. cv:, tr: and req: refs arrive with those features."""
    kind, _, rest = ref.partition(":")
    store = req.app.state.services.store
    if kind == "pol":
        item = store.get_item("policy", rest)
        if item:
            return {"ref": ref, "text": item["text"]}
    if kind == "cv":
        candidate_id, sep, chunk_id = rest.rpartition(":")
        cand = store.get_candidate(candidate_id) if sep else None
        if not cand:
            raise HTTPException(404, "Not found")
        require_job(store, user, cand["job_id"])
        item = store.get_item(f"cv:{candidate_id}", chunk_id)
        if item:
            return {"ref": ref, "text": item["text"]}
    raise HTTPException(404, "Not found")


@router.get("/audit")
def get_audit(req: Request, user: dict = Depends(require_roles("admin"))):
    return req.app.state.services.store.list_audit(200)


@router.get("/dashboard/summary")
def dashboard(req: Request, user: dict = Depends(staff)):
    owner = None if user["role"] == "admin" else user["sub"]
    return req.app.state.services.store.dashboard(owner)


@router.put("/jobs/{job_id}/weights")
def update_weights(job_id: str, body: WeightsIn, req: Request, user: dict = Depends(staff)):
    store = req.app.state.services.store
    job = require_job(store, user, job_id)
    w = body.weights
    if set(w) != set(job["weights"]) or any(v < 0 for v in w.values()) or sum(w.values()) <= 0:
        raise HTTPException(422, "Weights must cover the same dimensions, be positive, and not all zero.")
    total = sum(w.values())
    store.update_weights(job_id, {k: round(v / total, 3) for k, v in w.items()})
    audit(req, user["role"], "weights_updated", "job", job_id)
    return store.get_job(job_id)


@router.post("/logs/client", status_code=204)
def client_logs(body: ClientLogIn, user: dict = Depends(current_user)):
    clientlog.write(body.entries, user["role"])
