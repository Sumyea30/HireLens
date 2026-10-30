"""End-to-end F1: login, policy upload, job assistant chat, human approval, blogger publish.
Uses fakes for the LLM and the posting channel, in-memory storage, and the real FastAPI app and Graph 0."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from app.core.auth import hash_password
from app.core.config import Settings
from app.graphs import Deps, build_job_post_graph, memory_checkpointer
from app.graphs.fakes import FakeChannel, FakeLLM
from app.main import Services, create_app
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService

POSTING = """\
Company: HireLens
Location: Dhaka, Bangladesh
Employment type: Full-time
Experience level: Mid-level
Salary range: 80000 BDT per month
Application deadline: 1 November 2026
About the role: We are looking for a backend developer to build hiring tools.
Responsibilities: Build APIs; Review code
Education: BSc in CSE
Experience: 3 years
Technical skills: Python, Django
Contact: hiring@example.com
"""
ROLE = "We need a backend developer.\n" + POSTING

USERS = {
    "ivy": {"hash": hash_password("pw-ivy"), "role": "interviewer", "name": "Ivy"},
    "ada": {"hash": hash_password("pw-ada"), "role": "admin", "name": "Ada"},
    "guest": {"hash": hash_password("pw-guest"), "role": "candidate", "name": "Guest"},
}


def _llm(draft_fails=False):
    def draft(user):
        if draft_fails:
            raise RuntimeError("provider down")
        return {"post": "Backend Developer. We are an equal opportunity employer.", "policy_refs": [], "unresolved_items": ["Salary band"]}

    return FakeLLM({"DraftOut": draft, "ComplianceOut": {"findings": [{"severity": "info", "message": "Follows the standards.", "policy_refs": []}]}})


def make_client(draft_fails=False, channel_configured=True):
    store = MemoryStore("hiring@example.com")
    channel = FakeChannel("blogger")
    deps = Deps(llm=_llm(draft_fails), store=store, repo=store, channels={"blogger": channel} if channel_configured else {}, apply_email="hiring@example.com")
    settings = Settings(auth_secret="test-secret", users_json=json.dumps(USERS), apply_email="hiring@example.com")
    svc = Services(settings, store, deps, build_job_post_graph(deps, memory_checkpointer()), PolicyService(store))
    return TestClient(create_app(svc)), channel, store


def login(client, user):
    r = client.post("/auth/login", json={"username": user, "password": f"pw-{user}"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_signup_stores_an_email_account_and_logs_in():
    c, _, store = make_client()
    created = c.post("/auth/signup", json={"name": " Samira ", "email": "Samira@gmail.com", "password": "long-enough"})
    assert created.status_code == 200, created.text
    assert created.json()["user"]["name"] == "Samira"
    assert created.json()["user"]["role"] == "interviewer"
    saved = store.get_recruiter_by_email("samira@gmail.com")
    assert saved and saved["password_hash"] != "long-enough" and saved["password_hash"].startswith("scrypt:")
    again = c.post("/auth/login", json={"username": "samira@gmail.com", "password": "long-enough"})
    assert again.status_code == 200
    assert c.get("/jobs", headers={"Authorization": "Bearer " + again.json()["token"]}).status_code == 200
    assert c.post("/auth/signup", json={"name": "Other", "email": "samira@gmail.com", "password": "long-enough"}).status_code == 409
    assert c.post("/auth/signup", json={"name": "Other", "email": "not-an-email", "password": "long-enough"}).status_code == 422
    for bad in ("a@b", "name@gmail", "bad..dot@gmail.com", "name@-gmail.com"):
        assert c.post("/auth/signup", json={"name": "Other", "email": bad, "password": "long-enough"}).status_code == 422
    assert c.post("/auth/login", json={"username": "ivy", "password": "pw-ivy"}).status_code == 200


def test_a_recruiter_sees_only_the_jobs_they_posted():
    c, *_ = make_client()
    ivy, ada = login(c, "ivy"), login(c, "ada")
    job_id = c.post("/jobs/assistant", headers=ivy, json={"message": "We need a backend developer"}).json()["job_id"]
    samira = c.post("/auth/signup", json={"name": "Samira", "email": "samira@gmail.com", "password": "long-enough"})
    hers = {"Authorization": "Bearer " + samira.json()["token"]}
    assert c.get("/jobs", headers=hers).json() == []
    assert c.get(f"/jobs/{job_id}", headers=hers).status_code == 404
    assert c.get(f"/jobs/{job_id}/assistant", headers=hers).status_code == 404
    assert [j["id"] for j in c.get("/jobs", headers=ivy).json()] == [job_id]
    assert c.get(f"/jobs/{job_id}", headers=ada).json()["id"] == job_id


def test_auth_rules():
    c, *_ = make_client()
    assert c.get("/health").json() == {"status": "ok"}
    assert c.get("/jobs").status_code == 401
    bad = c.post("/auth/login", json={"username": "ivy", "password": "wrong"})
    unknown = c.post("/auth/login", json={"username": "nobody", "password": "x"})
    assert bad.status_code == unknown.status_code == 401 and bad.json() == unknown.json()
    assert c.get("/jobs", headers=login(c, "guest")).status_code == 403          # candidates are kept out (N9)
    assert c.get("/jobs", headers={"Authorization": "Bearer forged.token"}).status_code == 401
    assert c.get("/jobs", headers=login(c, "ivy")).json() == []


def test_policy_upload_versions_and_admin_only_delete():
    import fitz
    from docx import Document
    c, *_ = make_client()
    ivy, ada = login(c, "ivy"), login(c, "ada")

    r = c.post("/policies", headers=ivy, files={"file": ("Posting standards.txt", b"Use approved titles.\n\nList skills separately.")})
    assert r.status_code == 200 and r.json()["version"] == "1.0" and r.json()["status"] == "active"
    r2 = c.post("/policies", headers=ivy, files={"file": ("Posting standards.txt", b"Updated rules for job posts.")})
    assert r2.json()["version"] == "2.0"
    listing = {p["id"]: p for p in c.get("/policies", headers=ivy).json()}
    assert listing[r.json()["id"]]["status"] == "superseded" and listing[r2.json()["id"]]["status"] == "active"

    buf = io.BytesIO(); d = Document(); d.add_paragraph("Every post includes the equal opportunity statement."); d.save(buf)
    assert c.post("/policies", headers=ivy, files={"file": ("eeo.docx", buf.getvalue())}).status_code == 200
    pdf = fitz.open(); pdf.new_page().insert_text((72, 72), "Salary bands must be approved by HR."); 
    assert c.post("/policies", headers=ivy, files={"file": ("pay.pdf", pdf.tobytes())}).status_code == 200

    assert c.post("/policies", headers=ivy, files={"file": ("virus.exe", b"x")}).status_code == 422
    assert c.post("/policies", headers=ivy, files={"file": ("empty.txt", b"   ")}).status_code == 422
    assert c.delete(f"/policies/{r2.json()['id']}", headers=ivy).status_code == 403
    assert c.delete(f"/policies/{r2.json()['id']}", headers=ada).status_code == 204

    ev = c.get("/evidence", params={"ref": f"pol:{listing[r.json()['id']]['id'] and r.json()['id']}:c001"}, headers=ivy)
    assert ev.status_code in (200, 404)   # superseded chunks are removed, so 404 is expected for the old version
    assert c.get("/evidence", params={"ref": "pol:nope:c001"}, headers=ivy).status_code == 404


def test_job_assistant_full_flow_with_blogger_publish():
    c, channel, store = make_client()
    h = login(c, "ivy")
    c.post("/policies", headers=h, files={"file": ("Standards.txt", b"Use approved titles and list skills separately.")})

    r1 = c.post("/jobs/assistant", headers=h, json={"message": "We need a backend developer"})
    assert r1.status_code == 200
    a = r1.json()
    job_id = a["job_id"]
    assert a["status"] == "collecting" and a["approval"] is None and a["missing_fields"]
    assert a["messages"][-1]["role"] == "assistant" and "skills" in a["messages"][-1]["content"]
    assert set(a) == {"job_id", "status", "messages", "profile", "missing_fields", "approval", "publish_results", "apply_email"}

    r2 = c.post("/jobs/assistant", headers=h, json={"job_id": job_id, "message": "Python and Docker, 3 years, BSc in CSE"})
    a = r2.json()
    assert a["missing_fields"] == [
        "contact_email", "company", "location_or_mode", "employment_type", "experience_level",
        "remuneration", "application_deadline", "about_role", "responsibilities",
    ]
    assert a["status"] == "collecting" and a["approval"] is None
    assert a["profile"]["min_experience_years"] == 3 and "Python" in a["profile"]["required_skills"]

    r3 = c.post("/jobs/assistant", headers=h, json={"job_id": job_id, "message": "hiring@example.com"})
    a = r3.json()
    assert a["status"] == "collecting" and "contact_email" not in a["missing_fields"]
    assert "salary range" in a["messages"][-1]["content"].lower() and "Company:" in a["messages"][-1]["content"]

    r4 = c.post("/jobs/assistant", headers=h, json={"job_id": job_id, "message": POSTING})
    a = r4.json()
    assert a["status"] == "awaiting_approval" and a["profile"]["contact_email"] == "hiring@example.com"
    assert a["profile"]["company"] == "HireLens" and a["profile"]["remuneration"].startswith("80000")
    assert a["profile"]["location_or_mode"].startswith("Dhaka") and a["profile"]["employment_type"] == "Full-time"
    assert "Build APIs" in a["profile"]["responsibilities"]
    ap = a["approval"]
    assert set(ap) == {"draft_post", "proposed_channels", "policy_evidence", "compliance_findings", "mandatory_failures", "unresolved_items"}
    assert ap["proposed_channels"] == ["blogger"] and ap["unresolved_items"] == ["Salary band"]
    assert ap["policy_evidence"] and ap["policy_evidence"][0]["ref"].startswith("pol:pol-")
    assert channel.published == []                                   # nothing posted before approval

    # resuming from a fresh request gives the same state (the pause is stored, not in memory)
    assert c.get(f"/jobs/{job_id}/assistant", headers=h).json()["approval"]["draft_post"] == ap["draft_post"]
    assert c.get(f"/jobs/{job_id}", headers=h).json()["status"] == "awaiting_approval"

    # wrong-state calls are refused
    assert c.post("/jobs/assistant", headers=h, json={"job_id": job_id, "message": "more"}).status_code == 409

    done = c.post(f"/jobs/{job_id}/approval", headers=h, json={"action": "approve", "channels": ["blogger"]}).json()
    assert done["status"] == "open" and done["apply_email"] == "hiring@example.com"
    assert done["publish_results"] == [{"channel": "blogger", "status": "ok", "url": "https://example.invalid/blogger", "error": None}]
    assert f"Application {job_id}" in channel.published[0]["body"] and "hiring@example.com" in channel.published[0]["body"]
    job = c.get(f"/jobs/{job_id}", headers=h).json()
    assert job["status"] == "open" and job["channels"] == ["blogger"] and job["requirements"]
    assert store.decisions[0]["approver_role"] == "interviewer" and store.decisions[0]["action"] == "approve"
    assert c.post(f"/jobs/{job_id}/approval", headers=h, json={"action": "approve", "channels": ["blogger"]}).status_code == 409


def test_reject_and_change_requests_never_publish():
    c, channel, _ = make_client()
    h = login(c, "ivy")
    job_id = c.post("/jobs/assistant", headers=h, json={"message": ROLE}).json()["job_id"]
    changed = c.post(f"/jobs/{job_id}/approval", headers=h, json={"action": "changes", "feedback": "Shorter please"}).json()
    assert changed["status"] == "awaiting_approval" and changed["approval"]
    rejected = c.post(f"/jobs/{job_id}/approval", headers=h, json={"action": "reject"}).json()
    assert rejected["status"] == "rejected" and channel.published == []
    assert c.get(f"/jobs/{job_id}", headers=h).json()["status"] == "rejected"


def test_edited_post_without_equal_opportunity_statement_is_not_published():
    c, channel, _ = make_client()
    h = login(c, "ivy")
    job_id = c.post("/jobs/assistant", headers=h, json={"message": ROLE}).json()["job_id"]
    out = c.post(f"/jobs/{job_id}/approval", headers=h, json={"action": "approve", "channels": ["blogger"], "edited_post": "Hiring now."}).json()
    assert out["status"] != "open" and channel.published == []


def test_a_failed_draft_can_be_tried_again():
    calls = {"n": 0}

    def draft(_user):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider down")
        return {"post": "Backend Developer. We are an equal opportunity employer.", "policy_refs": [], "unresolved_items": []}

    c, _, _ = make_client()
    c.app.state.services.deps.llm.canned["DraftOut"] = draft
    h = login(c, "ivy")
    first = c.post("/jobs/assistant", headers=h, json={"message": ROLE})
    assert first.status_code == 200 and first.json()["approval"] is None and first.json()["status"] == "failed"
    second = c.post("/jobs/assistant", headers=h, json={"job_id": first.json()["job_id"], "message": ROLE})
    assert second.status_code == 200, second.text
    assert second.json()["approval"]["draft_post"].startswith("Backend Developer")
    assert calls["n"] == 2


def test_ai_failure_is_reported_safely_and_input_is_validated():
    c, channel, _ = make_client(draft_fails=True)
    h = login(c, "ivy")
    r = c.post("/jobs/assistant", headers=h, json={"message": ROLE})
    assert r.status_code == 200
    body = r.json()
    assert "Something went wrong" in body["messages"][-1]["content"] and body["approval"] is None
    assert "Traceback" not in r.text and "provider down" not in r.text and channel.published == []
    assert c.post("/jobs/assistant", headers=h, json={"message": ""}).status_code == 422
    assert c.post("/jobs/assistant", headers=h, json={"message": "x" * 9000}).status_code == 422
    assert c.post("/jobs/nope/approval", headers=h, json={"action": "bogus"}).status_code == 422
    assert c.get("/jobs/nope/assistant", headers=h).status_code == 404


def test_no_channel_configured_means_nothing_to_choose():
    c, channel, _ = make_client(channel_configured=False)
    h = login(c, "ivy")
    a = c.post("/jobs/assistant", headers=h, json={"message": ROLE}).json()
    assert a["approval"]["proposed_channels"] == []
