"""Graph A runs after intake: the upload stays queued, screening fills the candidate."""
import json

import pytest
from fastapi.testclient import TestClient

from app.graphs import Deps, build_cv_graph, build_job_post_graph, memory_checkpointer
from app.graphs.fakes import FakeLLM
from app.main import Services, create_app
from app.services.cvtext import extract_cv_text
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService
from app.services.screening import make_on_queued, screen_candidate
from tests.helpers_f2 import FACE_JPEG, PDF, make_cv_docx, make_docx, open_job, settings, wired_intake

CV = (
    "Jane Doe\n"
    "Email: jane@example.com\n"
    "Phone: +880 1700 000000\n"
    "Date of Birth: 1999-01-01\n"
    "Gender: F\n\n"
    "Experience\n"
    "Python developer with Docker experience across backend services. "
    "Five years of relevant experience building and running APIs.\n\n"
    "Education\n"
    "BSc in Computer Science. Shipped containerised Python services for production workloads "
    "and kept the Docker images small enough to deploy every week.\n"
)

def _cv_bytes(text=CV):
    return make_docx(text, photo=FACE_JPEG)



def _llm():
    def matches(user):
        payload = json.loads(user)
        out = []
        for req in payload["requirements"]:
            refs = [s["ref"] for s in payload["evidence"].get(req["id"], [])[:1]]
            out.append({"requirement_id": req["id"], "status": "met" if refs else "unmet",
                        "justification": "Checked against the CV.", "evidence_refs": refs})
        return {"matches": out}

    def scores(user):
        payload = json.loads(user)
        ref = next(m["evidence_refs"][0] for m in payload["matches"] if m["evidence_refs"])
        values = {"technical_skills": 80, "experience": 70, "qualifications": 60}
        return {"scores": [{"dimension": d, "score": values[d], "justification": f"{d} checked", "evidence_refs": [ref]}
                           for d in payload["dimensions"]]}

    return FakeLLM({
        "ParsedCV": {"skills": ["Python", "Docker"], "education": ["BSc in Computer Science"],
                     "work_history": ["Backend developer, 5 years"], "projects": ["Inventory API"],
                     "total_experience_years": 5},
        "MatchesOut": matches,
        "DimensionsOut": scores,
    })


def _no_ocr(_path):
    raise AssertionError("text was long enough; OCR should not run")


def make_client(tmp_path, llm=None, extract=extract_cv_text):
    store = MemoryStore("hiring@example.com")
    llm = llm or _llm()
    deps = Deps(llm=llm, store=store, repo=store, extract_text=extract, ocr_text=_no_ocr)
    s = settings(photo_dir=str(tmp_path / "photos"))
    intake, photos, face = wired_intake(tmp_path, store, on_queued=make_on_queued(s), settings_obj=s)
    svc = Services(s, store, deps, build_job_post_graph(deps, memory_checkpointer()), PolicyService(store), intake)
    svc.cv_graph = build_cv_graph(deps)
    svc.photos, svc.face = photos, face
    return TestClient(create_app(svc)), svc, llm


def login(c):
    return {"Authorization": "Bearer " + c.post("/auth/login", json={"username": "ivy", "password": "pw-ivy"}).json()["token"]}


def test_extract_reads_docx_paragraphs(tmp_path):
    path = tmp_path / "cv.docx"
    path.write_bytes(make_docx("Python and Docker on the first page."))
    assert "Python and Docker" in extract_cv_text(str(path))
    assert len(CV) >= 200


def test_upload_stays_queued_until_screening_fills_the_candidate(tmp_path):
    c, svc, _ = make_client(tmp_path)
    h = login(c)
    job = open_job(svc.store)
    up = c.post(f"/jobs/{job}/cvs", headers=h, files=[("files", ("cv.docx", _cv_bytes(), "application/zip"))])
    assert up.status_code == 200 and up.json()[0]["status"] == "queued"
    cid = c.get(f"/candidates?job_id={job}", headers=h).json()[0]["id"]
    queued = c.get(f"/candidates/{cid}", headers=h).json()
    assert queued["status"] == "queued" and queued["parsed"]["skills"] == [] and queued["matches"] == []
    assert queued["email"] == "jane@example.com" and queued["aggregate_score"] is None

    assert screen_candidate(svc, cid) == "completed"
    detail = c.get(f"/candidates/{cid}", headers=h).json()
    assert detail["status"] == "screened" and detail["aggregate_score"] == 73.0
    assert detail["parsed"]["skills"] == ["Python", "Docker"]
    assert detail["parsed"]["total_experience_years"] == 5
    assert detail["email"] == "jane@example.com" and detail["phone"] is None
    assert {m["status"] for m in detail["matches"]} == {"met"} and all(m["requirement_text"] and m["evidence"] for m in detail["matches"])
    assert {s["dimension"]: s["weight"] for s in detail["dimension_scores"]} == {
        "technical_skills": 0.5, "experience": 0.3, "qualifications": 0.2}
    blob = json.dumps({k: v for k, v in detail.items() if k != "email"})
    assert "1999" not in blob and "Gender" not in blob and "jane@example.com" not in blob
    ref = detail["matches"][0]["evidence"][0]["ref"]
    shown = c.get("/evidence", headers=h, params={"ref": ref}).json()
    assert shown["ref"] == ref and "Python" in shown["text"] and "1999" not in shown["text"]
    assert c.get("/dashboard/summary", headers=h).json()["awaiting_screening"] == 0
    assert screen_candidate(svc, cid) == "skipped"
    admin = {"Authorization": "Bearer " + c.post("/auth/login", json={"username": "ada", "password": "pw-ada"}).json()["token"]}
    assert any(e["action"] == "cv_screened" and e["entity_id"] == cid for e in c.get("/audit", headers=admin).json())


def test_email_is_taken_from_the_cv_not_the_message_sender(tmp_path):
    _c, svc, _ = make_client(tmp_path)
    job = open_job(svc.store)
    res = svc.intake.accept(job_id=job, filename="cv.docx", data=_cv_bytes(), source="email",
                            message_id_hash="hash-1", sender_email="other@example.com")
    assert res["status"] == "queued"
    assert screen_candidate(svc, res["candidate_id"]) == "completed"
    cand = svc.store.get_candidate(res["candidate_id"])
    assert cand["email"] == "jane@example.com" and cand["phone"] is None and cand["name"] == "Jane Doe"


def test_unreadable_cv_is_marked_failed_without_calling_the_model(tmp_path):
    def boom(_path):
        raise ValueError("corrupt")

    c, svc, llm = make_client(tmp_path, extract=boom)
    h = login(c)
    job = open_job(svc.store)
    cid = c.post(f"/jobs/{job}/cvs", headers=h, files=[("files", ("a.docx", _cv_bytes(), "application/zip"))]).json()
    assert cid[0]["status"] == "queued"
    cand_id = c.get("/candidates", headers=h).json()[0]["id"]
    assert screen_candidate(svc, cand_id) == "failed" and llm.calls == []
    detail = c.get(f"/candidates/{cand_id}", headers=h).json()
    assert detail["status"] == "failed" and detail["parsed"]["skills"] == [] and detail["matches"] == []
    assert "could not be read" in detail["failure_reason"]


def test_a_job_with_no_requirements_fails_screening(tmp_path):
    _c, svc, _ = make_client(tmp_path)
    svc.store.create_job("job-empty")
    svc.store.update_job("job-empty", status="open")
    res = svc.intake.accept(job_id="job-empty", filename="cv.docx", data=_cv_bytes(), source="upload")
    assert screen_candidate(svc, res["candidate_id"]) == "failed"
    assert svc.store.get_candidate(res["candidate_id"])["failure_reason"] == "Screening could not be completed."


def test_the_same_cv_keeps_its_score_and_the_review_explains_why(tmp_path):
    c, svc, llm = make_client(tmp_path)
    h = login(c)
    job = open_job(svc.store)
    files = [("files", ("cv.docx", _cv_bytes(), "application/zip"))]
    first = c.post(f"/jobs/{job}/cv-batch", headers=h, files=files)
    assert first.status_code == 200
    body = first.json()
    assert body["files"][0]["status"] == "screened"
    score = body["files"][0]["aggregate_score"]
    assert score == 73.0
    review = body["review"]
    assert review["candidates"][0]["rank"] == 1
    assert review["candidates"][0]["met"] >= 1
    assert "same file again" in review["why_these"]
    assert "Met:" in review["candidates"][0]["why"]
    calls = len(llm.calls)

    second = c.post(f"/jobs/{job}/cv-batch", headers=h, files=files)
    assert second.status_code == 200
    assert second.json()["files"][0]["aggregate_score"] == score
    assert len(llm.calls) == calls
    again = c.get(f"/jobs/{job}/cv-review", headers=h).json()
    assert [row["aggregate_score"] for row in again["candidates"]] == [score, score]
    assert "same file again" in again["why_these"]
    tails = [row["why"].split(" Gemini marked", 1)[1] for row in again["candidates"]]
    assert tails[0] == tails[1]


def test_a_batch_scores_cvs_at_the_same_time(tmp_path, monkeypatch):
    import threading
    from app.services import screening

    c, svc, _ = make_client(tmp_path)
    h = login(c)
    job = open_job(svc.store)
    gate = threading.Barrier(2)
    original = screening.screen_candidate

    def together(services, candidate_id):
        gate.wait(timeout=3)
        return original(services, candidate_id)

    monkeypatch.setattr(screening, "screen_candidate", together)
    files = [
        ("files", ("a.docx", _cv_bytes(), "application/zip")),
        ("files", ("b.docx", _cv_bytes(CV + "\n\nExtra project work on APIs."), "application/zip")),
    ]
    res = c.post(f"/jobs/{job}/cv-batch", headers=h, files=files)
    assert res.status_code == 200
    assert [row["status"] for row in res.json()["files"]] == ["screened", "screened"]


def test_a_recruiter_cannot_score_cvs_for_someone_elses_job(tmp_path):
    c, svc, _ = make_client(tmp_path)
    job = open_job(svc.store)
    other = c.post("/auth/signup", json={"name": "Samira", "email": "samira@example.com", "password": "long-enough"}).json()["token"]
    files = [("files", ("cv.docx", _cv_bytes(), "application/zip"))]
    denied = c.post(f"/jobs/{job}/cv-batch", headers={"Authorization": "Bearer " + other}, files=files)
    assert denied.status_code == 404
    ada = c.post("/auth/login", json={"username": "ada", "password": "pw-ada"}).json()["token"]
    allowed = c.post(f"/jobs/{job}/cv-batch", headers={"Authorization": "Bearer " + ada}, files=files)
    assert allowed.status_code == 200 and allowed.json()["files"][0]["status"] == "screened"


def test_review_states_what_was_met_partially_met_and_not_met():
    from app.services.review import explain_candidate

    text = explain_candidate("Applicant ABCD", 70, [
        {"requirement_text": "Python", "status": "met", "justification": "The CV lists Python."},
        {"requirement_text": "Docker", "status": "partial", "justification": "Docker is only mentioned once."},
        {"requirement_text": "BSc", "status": "unmet", "justification": "No degree is listed."},
    ])
    assert "scored 70" in text
    assert "partially met" in text and "Docker is only mentioned once." in text
    assert "not met" in text and "No degree is listed." in text
    assert "The CV lists Python." in text


def test_worker_module_exposes_the_celery_app():
    from app.workers.celery_app import app, celery_app
    assert app is celery_app
    assert "hirelens.screen_candidate" in app.tasks


def test_postgres_save_and_load_roundtrip():
    import os
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    from app.graphs.fakes import FakeVectorStore
    from app.services.pgstore import PgStore

    class HashEmbedder:
        def embed(self, texts, task="RETRIEVAL_DOCUMENT"):
            return [FakeVectorStore._vec(t) for t in texts]

    schema = "f3screen"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        store = PgStore(f"{url}{'&' if '?' in url else '?'}options=-c%20search_path%3D{schema}%2Cpublic",
                        HashEmbedder(), 256, "hiring@example.com")
        store.init_schema()
        job = open_job(store, "job-pg")
        store.create_candidate({"id": "cand-pg", "job_id": job, "source": "upload", "file_path": "/data/cand-pg.docx"})
        assert store.claim_candidate("cand-pg")["file_path"].endswith(".docx")
        reqs = store.get_job(job)["requirements"]
        ref = "cv:cand-pg:c001"
        store.index("cv:cand-pg", [{"id": "c001", "text": "Python and Docker"}])
        store.save_cv_result("cand-pg", job, {
            "status": "completed", "aggregate_score": 73.0, "prompt_version": "cv-v1",
            "contact": {"email": "jane@example.com", "phone": "+880 1700 000000"},
            "parsed_cv": {"skills": ["Python"], "education": [], "work_history": [], "certifications": [],
                          "projects": [], "total_experience_years": 5},
            "weights": {"technical_skills": 0.5, "experience": 0.3, "qualifications": 0.2},
            "matches": [{"requirement_id": r["id"], "status": "met", "justification": "Shown in the CV.",
                         "evidence_refs": [ref]} for r in reqs],
            "dimension_scores": [
                {"dimension": "technical_skills", "score": 80, "justification": "Stack match", "evidence_refs": [ref]},
                {"dimension": "experience", "score": 70, "justification": "Years", "evidence_refs": [ref]},
                {"dimension": "qualifications", "score": 60, "justification": "Degree", "evidence_refs": [ref]},
            ],
        })
        loaded = store.load_cv_result("cand-pg", job)
        assert loaded["status"] == "completed" and loaded["aggregate_score"] == 73.0
        assert loaded["parsed_cv"]["skills"] == ["Python"]
        assert {m["requirement_id"] for m in loaded["matches"]} == {r["id"] for r in reqs}
        cand = store.get_candidate("cand-pg")
        assert cand["status"] == "screened" and cand["email"] is None and cand["phone"] is None
        assert store.claim_candidate("cand-pg") is None
        assert "cand-pg" not in store.list_pending_screening()
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
