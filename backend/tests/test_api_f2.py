"""F2 through the real API: CV upload, candidates, deletion with all data, audit and dashboard."""
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.graphs import Deps, build_job_post_graph, memory_checkpointer
from app.graphs.fakes import FakeLLM
from app.main import Services, create_app
from app.services.intake import CVStorage, IntakeService
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService
from tests.helpers_f2 import PDF, make_docx, open_job, settings


def make_client(tmp_path, max_mb=10):
    store = MemoryStore("hiring@example.com")
    deps = Deps(llm=FakeLLM({}), store=store, repo=store)
    intake = IntakeService(store, CVStorage(str(tmp_path / "cvs")), max_mb)
    svc = Services(settings(), store, deps, build_job_post_graph(deps, memory_checkpointer()), PolicyService(store), intake)
    return TestClient(create_app(svc)), store, tmp_path / "cvs"


def login(c, user):
    return {"Authorization": "Bearer " + c.post("/auth/login", json={"username": user, "password": f"pw-{user}"}).json()["token"]}


def up(c, h, job_id, *files):
    return c.post(f"/jobs/{job_id}/cvs", headers=h, files=[("files", f) for f in files])


def test_upload_stores_files_under_generated_names_and_lists_candidates(tmp_path):
    c, store, cvdir = make_client(tmp_path)
    h = login(c, "ivy")
    job = open_job(store)
    r = up(c, h, job, ("Jane Doe CV.pdf", PDF, "application/pdf"), ("resume.docx", make_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    assert r.status_code == 200 and [x["status"] for x in r.json()] == ["queued", "queued"]
    files = sorted(p.name for p in cvdir.iterdir())
    assert len(files) == 2 and all(f.startswith("cand-") for f in files) and not any("Jane" in f or "resume" in f for f in files)

    cands = c.get(f"/candidates?job_id={job}", headers=h).json()
    assert len(cands) == 2 and {x["status"] for x in cands} == {"queued"} and {x["source"] for x in cands} == {"upload"}
    assert all(x["name"].startswith("Applicant ") and "Jane" not in x["name"] for x in cands)
    detail = c.get(f"/candidates/{cands[0]['id']}", headers=h).json()
    assert detail["matches"] == [] and detail["parsed"]["skills"] == [] and detail["email"] is None
    assert c.get("/candidates/nope", headers=h).status_code == 404
    assert c.get(f"/jobs/{job}", headers=h).json()["applicants"] == 2
    assert sum(1 for e in c.get("/audit", headers=h).json() if e["action"] == "cv_received") == 2


def test_bad_files_are_rejected_with_a_reason_and_nothing_is_stored(tmp_path):
    c, store, cvdir = make_client(tmp_path, max_mb=1)
    h = login(c, "ivy")
    job = open_job(store)
    big = PDF + b"x" * (1024 * 1024)
    r = up(c, h, job, ("virus.exe", b"MZ", "application/octet-stream"), ("fake.pdf", b"not a pdf", "application/pdf"),
           ("empty.pdf", b"", "application/pdf"), ("big.pdf", big, "application/pdf"), ("fake.docx", b"PK-not-really", "application/zip"),
           ("notes.docx", b"", "application/zip"))
    results = r.json()
    assert all(x["status"] == "rejected" and x["reason"] for x in results)
    assert "larger than 1 MB" in results[3]["reason"]
    assert list(cvdir.iterdir()) == [] and c.get("/candidates", headers=h).json() == []


def test_job_rules_and_input_limits(tmp_path):
    c, store, _ = make_client(tmp_path)
    h = login(c, "ivy")
    store.create_job("job-draft1", "ivy")                                         # still a draft: not open
    assert up(c, h, "job-draft1", ("a.pdf", PDF, "application/pdf")).json()[0]["reason"] == "This job is not open for applications."
    assert up(c, h, "job-nope", ("a.pdf", PDF, "application/pdf")).status_code == 404
    job = open_job(store)
    assert c.post(f"/jobs/{job}/cvs", headers=h, files=[]).status_code == 422
    assert up(c, h, job, *[(f"{i}.pdf", PDF, "application/pdf") for i in range(21)]).status_code == 422


def test_access_control_and_deletion_removes_everything(tmp_path):
    c, store, cvdir = make_client(tmp_path)
    ivy, ada, guest = login(c, "ivy"), login(c, "ada"), login(c, "guest")
    job = open_job(store)
    assert c.post(f"/jobs/{job}/cvs", files=[("files", ("a.pdf", PDF, "application/pdf"))]).status_code == 401
    assert up(c, guest, job, ("a.pdf", PDF, "application/pdf")).status_code == 403
    assert c.get("/candidates", headers=guest).status_code == 403
    cid = up(c, ivy, job, ("a.pdf", PDF, "application/pdf")).json() and c.get("/candidates", headers=ivy).json()[0]["id"]
    other = c.post("/auth/signup", json={"name": "Samira", "email": "samira@gmail.com", "password": "long-enough"})
    hers = {"Authorization": "Bearer " + other.json()["token"]}
    assert c.get("/jobs", headers=hers).json() == []
    assert c.get(f"/candidates/{cid}", headers=hers).status_code == 404
    assert c.get(f"/candidates/{cid}", headers=ada).status_code == 200
    store.index(f"cv:{cid}", [{"id": "c001", "text": "Python developer"}])
    assert len(list(cvdir.iterdir())) == 1

    assert c.delete(f"/admin/candidates/{cid}", headers=ivy).status_code == 403          # only an admin may delete
    assert c.delete(f"/admin/candidates/{cid}", headers=ada).status_code == 204
    assert list(cvdir.iterdir()) == []                                                   # the file is gone too
    assert c.get(f"/candidates/{cid}", headers=ivy).status_code == 404
    assert store.search(f"cv:{cid}", "Python", 3) == []                                  # and its vectors
    assert c.delete(f"/admin/candidates/{cid}", headers=ada).status_code == 404
    events = c.get("/audit", headers=ivy).json()
    assert any(e["action"] == "candidate_deleted" and e["entity_id"] == cid and e["actor_role"] == "admin" for e in events)


def test_audit_and_dashboard_hold_no_usernames(tmp_path):
    c, store, _ = make_client(tmp_path)
    c.post("/auth/login", json={"username": "ivy", "password": "wrong"})
    h = login(c, "ivy")
    job = open_job(store)
    up(c, h, job, ("a.pdf", PDF, "application/pdf"))
    events = c.get("/audit", headers=h).json()
    assert {"login_failed", "login", "cv_received"} <= {e["action"] for e in events}
    assert "ivy" not in str(events)                                                      # users are identified by a short hash
    d = c.get("/dashboard/summary", headers=h).json()
    assert d["open_jobs"] == 1 and d["applications_total"] == 1 and d["awaiting_screening"] == 1
    assert [p["stage"] for p in d["pipeline"]] == ["Received", "Screened", "Interviewed", "Assessed"] and d["recent"]


def test_weights_validation_and_normalisation(tmp_path):
    c, store, _ = make_client(tmp_path)
    h = login(c, "ivy")
    job = open_job(store)
    ok = c.put(f"/jobs/{job}/weights", headers=h, json={"weights": {"technical_skills": 2, "experience": 1, "qualifications": 1}})
    assert ok.status_code == 200 and ok.json()["weights"] == {"technical_skills": 0.5, "experience": 0.25, "qualifications": 0.25}
    for bad in [{"technical_skills": 1}, {"technical_skills": -1, "experience": 1, "qualifications": 1}, {"technical_skills": 0, "experience": 0, "qualifications": 0}]:
        assert c.put(f"/jobs/{job}/weights", headers=h, json={"weights": bad}).status_code == 422
    assert c.put("/jobs/nope/weights", headers=h, json={"weights": {"a": 1}}).status_code == 404


def test_browser_logs_are_sanitised_again_on_the_server(tmp_path, caplog):
    import json, logging
    c, _, _ = make_client(tmp_path)
    h = login(c, "ivy")
    entries = [{"ts": "2026-10-07T10:00:00Z", "level": "info", "event": "page_view", "ctx": {"path": "/jobs", "email": "a@b.c", "token": "x", "n": 3, "nested": {"a": 1}}},
               {"level": "hacker", "event": "bad_level", "ctx": {}}, {"level": "info", "ctx": {}}]
    with caplog.at_level(logging.INFO, logger="hirelens.client"):
        assert c.post("/logs/client", headers=h, json={"entries": entries}).status_code == 204
    lines = [json.loads(r.message) for r in caplog.records if r.name == "hirelens.client"]
    assert lines == [{"ts": "2026-10-07T10:00:00Z", "role": "interviewer", "level": "info", "event": "page_view", "ctx": {"path": "/jobs", "n": 3}}]
    assert c.post("/logs/client", json={"entries": []}).status_code == 401
    assert c.post("/logs/client", headers=h, json={"entries": [{}] * 201}).status_code == 422


def test_dev_mode_uses_the_real_embedder_for_policy_search():
    seen = []
    class Spy:
        def embed(self, texts, task="RETRIEVAL_DOCUMENT"):
            seen.append(task)
            return [[1.0, 0.0] if "titles" in t else [0.0, 1.0] for t in texts]
    s = MemoryStore(embedder=Spy())
    s.add("policy", "pol-1", [{"id": "pol-1:c001", "text": "approved titles"}, {"id": "pol-1:c002", "text": "parking rules"}])
    assert s.search("policy", "which titles", 1)[0]["id"] == "pol-1:c001"
    assert set(seen) == {"RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"}
