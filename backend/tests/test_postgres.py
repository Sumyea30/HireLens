"""Runs only when TEST_DATABASE_URL points at a Postgres with pgvector. Each test gets a throw-away schema.
Example: TEST_DATABASE_URL=postgresql://hl:hlpass@localhost:5432/hl python -m pytest tests/test_postgres.py"""
import json
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.graphs import Deps, build_job_post_graph
from app.graphs.fakes import FakeChannel, FakeLLM, FakeVectorStore
from app.main import Services, create_app
from app.services.intake import CVStorage, IntakeService
from app.services.memory_store import DuplicateApplication
from app.services.pgstore import PgStore
from app.services.policy import PolicyService
from tests.helpers_f2 import PDF, make_docx, open_job, settings
from tests.test_api_f1 import POSTING

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
DIMS = 256   # matches FakeVectorStore's hashing embedder


class HashEmbedder:
    def embed(self, texts, task="RETRIEVAL_DOCUMENT"):
        return [FakeVectorStore._vec(t) for t in texts]


class BrokenEmbedder:
    def embed(self, texts, task="RETRIEVAL_DOCUMENT"):
        raise RuntimeError("provider down")


@pytest.fixture()
def url():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS f2test CASCADE")
        c.execute("CREATE SCHEMA f2test")
    yield f"{URL}{'&' if '?' in URL else '?'}options=-c%20search_path%3Df2test%2Cpublic"
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA f2test CASCADE")


@pytest.fixture()
def store(url):
    s = PgStore(url, HashEmbedder(), DIMS, "hiring@example.com")
    s.init_schema()
    s.init_schema()          # idempotent
    return s


def count(store, sql, *args):
    with store._conn() as c:
        return c.execute(sql, args).fetchone()["n"]


def test_old_v1_database_is_detected_with_a_clear_message(url):
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("CREATE TABLE vector_item (collection text, item_id text, doc_id text)")
    with pytest.raises(RuntimeError, match="docker compose down -v"):
        PgStore(url, HashEmbedder(), DIMS).init_schema()


def test_policy_versions_swap_atomically_and_failures_keep_the_old_version(store):
    old = {"id": "pol-1", "title": "Standards", "version": "1.0", "effective_date": "2026-01-01T00:00:00+00:00", "status": "active", "chunks": 1}
    store.publish_policy(old, [{"id": "pol-1:c001", "text": "Use approved job titles"}])
    new = {**old, "id": "pol-2", "version": "2.0", "effective_date": "2026-02-01T00:00:00+00:00"}
    store.publish_policy(new, [{"id": "pol-2:c001", "text": "Use approved titles and list skills"}], supersede_id="pol-1")
    assert {p["id"]: p["status"] for p in store.list_policies()} == {"pol-1": "superseded", "pol-2": "active"}
    assert [h["id"] for h in store.search("policy", "approved titles", 5)] == ["pol-2:c001"]      # old chunks removed

    broken = PgStore(store.url, BrokenEmbedder(), DIMS)
    with pytest.raises(RuntimeError):
        broken.publish_policy({**old, "id": "pol-3", "version": "3.0"}, [{"id": "pol-3:c001", "text": "x"}], supersede_id="pol-2")
    assert store.find_active_policy("Standards")["id"] == "pol-2"                                  # a failure changes nothing
    with pytest.raises(psycopg.errors.UniqueViolation):                                            # the database also refuses two actives
        with store._conn() as c:
            c.execute("INSERT INTO policy_document VALUES ('pol-9','Standards','9.0',now(),'active',1)")
    store.delete_policy("pol-2")
    assert store.search("policy", "titles", 5) == []                                               # chunks cascade


def test_jobs_requirements_are_normalised_and_locked_once_open(store):
    store.create_job("job-1")
    profile = {"title": "Backend Developer", "required_skills": ["Python", "Docker"], "min_experience_years": 3, "qualifications": ["BSc"]}
    store.update_job("job-1", title="Backend Developer", status="awaiting_approval", profile=profile)
    job = store.get_job("job-1")
    assert [r["text"] for r in job["requirements"]] == ["Python", "Docker", "BSc", "At least 3 years of relevant experience"]
    assert job["profile"]["title"] == "Backend Developer" and job["applicants"] == 0
    store.update_job("job-1", status="open")
    store.update_job("job-1", title="Hacked", profile={"title": "Hacked", "required_skills": ["Cooking"]})   # ignored: job is open
    job = store.get_job("job-1")
    assert job["title"] == "Backend Developer" and len(job["requirements"]) == 4
    assert [j["id"] for j in store.list_jobs()] == ["job-1"] and store.get_job("nope") is None


def test_candidates_applications_and_cascading_deletion(store):
    open_job(store, "job-1")
    app = {"message_id_hash": "h1"}
    store.create_candidate({"id": "cand-1", "job_id": "job-1", "email": "a@example.org", "source": "email", "file_path": "/data/cand-1.pdf"}, app)
    with pytest.raises(DuplicateApplication):                                                       # same email again: rolled back
        store.create_candidate({"id": "cand-2", "job_id": "job-1", "source": "email"}, app)
    assert store.get_candidate("cand-2") is None and store.application_seen("h1")
    assert store.record_rejected_application("h2", None, "no_job_code") and not store.record_rejected_application("h2", None, "no_job_code")
    store.create_candidate({"id": "cand-3", "job_id": "job-1", "source": "upload", "file_path": "/data/cand-3.pdf"})
    store.index("cv:cand-1", [{"id": "c001", "text": "Built Django services"}])
    assert store.get_job("job-1")["applicants"] == 2
    listed = store.list_candidates("job-1")
    assert {c["id"] for c in listed} == {"cand-1", "cand-3"} and all(c["assessment_id"] is None for c in listed)
    assert store.get_candidate("cand-1")["email"] == "a@example.org"

    d = store.dashboard()
    assert d["applications_total"] == 2 and d["awaiting_screening"] == 2 and d["open_jobs"] == 1

    assert store.delete_candidate("cand-1") == "/data/cand-1.pdf"
    assert store.delete_candidate("cand-1") is None
    assert not store.application_seen("h1") and store.search("cv:cand-1", "Django", 3) == []        # application and vectors went too
    assert store.get_job("job-1") is not None and store.get_candidate("cand-3") is not None          # nothing else was touched


def test_audit_log_orders_newest_first(store):
    for i in range(3):
        store.add_audit("system", "step", "job", f"job-{i}")
    assert [e["entity_id"] for e in store.list_audit(2)] == ["job-2", "job-1"]
    with pytest.raises(psycopg.errors.CheckViolation):
        store.add_audit("hacker", "x", "y", "z")


def _llm():
    return FakeLLM({"DraftOut": {"post": "Backend Developer. Equal opportunity employer.", "policy_refs": [], "unresolved_items": []},
                    "ComplianceOut": {"findings": []}})


def _services(url, tmp_path, store):
    from langgraph.checkpoint.postgres import PostgresSaver
    channel = FakeChannel("blogger")
    deps = Deps(llm=_llm(), store=store, repo=store, channels={"blogger": channel}, apply_email="hiring@example.com")
    cm = PostgresSaver.from_conn_string(url)
    saver = cm.__enter__()
    saver.setup()
    svc = Services(settings(), store, deps, build_job_post_graph(deps, saver), PolicyService(store),
                   IntakeService(store, CVStorage(str(tmp_path / "cvs")), 10))
    svc.stack.callback(lambda: cm.__exit__(None, None, None))
    return svc, channel


def login(c):
    return {"Authorization": "Bearer " + c.post("/auth/login", json={"username": "ivy", "password": "pw-ivy"}).json()["token"]}


def test_whole_flow_on_postgres_across_restarts(url, store, tmp_path):
    """Chat -> restart -> draft -> restart -> approve and publish -> upload a CV -> admin deletes it."""
    svc1, ch1 = _services(url, tmp_path, store)
    c1 = TestClient(create_app(svc1)); h = login(c1)
    c1.post("/policies", headers=h, files={"file": ("Standards.txt", b"Use approved titles and list skills separately.")})
    job_id = c1.post("/jobs/assistant", headers=h, json={"message": "We need a backend developer"}).json()["job_id"]
    svc1.stack.close()

    svc2, _ = _services(url, tmp_path, store)
    c2 = TestClient(create_app(svc2))
    a = c2.post("/jobs/assistant", headers=h, json={"job_id": job_id, "message": POSTING}).json()
    assert a["status"] == "awaiting_approval" and a["approval"]["policy_evidence"]
    assert c2.post(f"/jobs/{job_id}/cvs", headers=h, files=[("files", ("a.pdf", PDF, "application/pdf"))]).json()[0]["status"] == "rejected"   # not open yet
    svc2.stack.close()

    svc3, ch3 = _services(url, tmp_path, store)
    c3 = TestClient(create_app(svc3))
    done = c3.post(f"/jobs/{job_id}/approval", headers=h, json={"action": "approve", "channels": ["blogger"]}).json()
    assert done["status"] == "open" and len(ch3.published) == 1 and ch1.published == []
    job = c3.get(f"/jobs/{job_id}", headers=h).json()
    assert job["status"] == "open" and job["channels"] == ["blogger"] and job["requirements"] and "Application" in job["post_text"]
    assert count(store, "SELECT count(*) AS n FROM posting_decision WHERE job_id=%s AND action='approve' AND approver_role='interviewer'", job_id) == 1
    assert count(store, "SELECT count(*) AS n FROM publish_result WHERE job_id=%s AND status='ok'", job_id) == 1

    up = c3.post(f"/jobs/{job_id}/cvs", headers=h, files=[("files", ("a.pdf", PDF, "application/pdf")), ("files", ("b.docx", make_docx(), "application/zip"))]).json()
    assert [x["status"] for x in up] == ["queued", "queued"]
    cands = c3.get(f"/candidates?job_id={job_id}", headers=h).json()
    assert len(cands) == 2 and c3.get("/dashboard/summary", headers=h).json()["applications_total"] == 2
    assert len(list((tmp_path / "cvs").iterdir())) == 2

    admin = {"Authorization": "Bearer " + c3.post("/auth/login", json={"username": "ada", "password": "pw-ada"}).json()["token"]}
    store.index(f"cv:{cands[0]['id']}", [{"id": "c001", "text": "Python developer"}])
    assert c3.delete(f"/admin/candidates/{cands[0]['id']}", headers=admin).status_code == 204
    assert len(list((tmp_path / "cvs").iterdir())) == 1 and count(store, "SELECT count(*) AS n FROM vector_item WHERE collection LIKE 'cv:%%'") == 0
    assert {e["action"] for e in c3.get("/audit", headers=h).json()} >= {"job_created", "post_approved", "cv_received", "candidate_deleted", "policy_uploaded"}
    svc3.stack.close()
