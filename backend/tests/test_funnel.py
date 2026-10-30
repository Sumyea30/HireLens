"""Funnel Doctor (F11): labels, coverage, what-if/near-miss, access, and prompt hygiene."""
import json

from fastapi.testclient import TestClient

from app.graphs import Deps, build_job_post_graph, memory_checkpointer
from app.graphs.fakes import FakeLLM
from app.main import Services, create_app
from app.services.funnel import (
    aggregates_for_llm,
    coverage_estimate,
    funnel_report,
    label_requirement,
)
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService
from tests.helpers_f2 import open_job, settings, wired_intake


def _seed_matches(store, job_id, rows):
    """rows: list of (cid, score, {req_id: status})."""
    for cid, score, statuses in rows:
        store.create_candidate({"id": cid, "job_id": job_id, "source": "upload", "file_path": None})
        matches = []
        for rid, status in statuses.items():
            matches.append({
                "requirement_id": rid,
                "status": status,
                "justification": "test",
                "evidence_refs": [f"cv:{cid}:c001"] if status != "unmet" else [],
            })
        store.save_cv_result(cid, job_id, {
            "status": "completed",
            "aggregate_score": score,
            "parsed_cv": {"skills": []},
            "matches": matches,
            "dimension_scores": [
                {"dimension": "technical_skills", "score": score, "justification": "t", "evidence_refs": []},
                {"dimension": "experience", "score": score, "justification": "t", "evidence_refs": []},
                {"dimension": "qualifications", "score": score, "justification": "t", "evidence_refs": []},
            ],
            "weights": {"technical_skills": 0.5, "experience": 0.3, "qualifications": 0.2},
            "prompt_version": "cv-v1",
        })


def _job_with_preferred(store, job_id="job-funnel1", owner_id="ivy"):
    store.create_job(job_id, owner_id)
    profile = {
        "title": "Backend Developer",
        "required_skills": ["Python", "Docker"],
        "preferred_skills": ["Kubernetes"],
        "min_experience_years": 3,
        "qualifications": ["BSc"],
    }
    store.update_job(job_id, title="Backend Developer", profile=profile)
    store.update_job(job_id, status="open")
    return job_id, store.get_job(job_id)


# ---- pure arithmetic -------------------------------------------------------

def test_label_thresholds(monkeypatch):
    monkeypatch.delenv("FUNNEL_BOTTLENECK_MAX", raising=False)
    monkeypatch.delenv("FUNNEL_FLAT_MIN", raising=False)
    assert label_requirement(0.19, bottleneck_max=0.2, flat_min=0.9) == "bottleneck"
    assert label_requirement(0.2, bottleneck_max=0.2, flat_min=0.9) == "balanced"
    assert label_requirement(0.9, bottleneck_max=0.2, flat_min=0.9) == "balanced"
    assert label_requirement(0.91, bottleneck_max=0.2, flat_min=0.9) == "non_discriminating"


def test_coverage_estimate():
    assert coverage_estimate([]) == 0.0
    assert coverage_estimate(["met", "met"]) == 1.0
    assert coverage_estimate(["met", "partial", "unmet"]) == round((1 + 0.5 + 0) / 3, 4)
    assert coverage_estimate(["unmet", "unmet"]) == 0.0


def test_what_if_and_near_miss_arithmetic(monkeypatch):
    monkeypatch.setenv("FUNNEL_SHORTLIST_N", "2")
    store = MemoryStore()
    job_id, job = _job_with_preferred(store)
    ids = [r["id"] for r in job["requirements"]]
    pref = next(r["id"] for r in job["requirements"] if r["kind"] == "preferred")
    mandatory0 = next(r["id"] for r in job["requirements"] if r["kind"] == "required")

    def statuses(**overrides):
        base = {rid: "met" for rid in ids}
        base.update(overrides)
        return base

    # Score shortlist = A, B. B has a mandatory partial so coverage < 1.
    # C only misses the preferred skill; flipping it lets C displace B on coverage rank.
    _seed_matches(store, job_id, [
        ("cand-a", 90, statuses()),
        ("cand-b", 80, statuses(**{mandatory0: "partial"})),
        ("cand-c", 70, statuses(**{pref: "unmet"})),
        ("cand-d", 40, statuses(**{mandatory0: "unmet", pref: "unmet"})),
    ])
    report = funnel_report(store, job_id)
    assert [c["id"] for c in report["shortlist"]] == ["cand-a", "cand-b"]
    assert report["low_sample"] is True
    assert all(r["mandatory"] is (r["kind"] == "required") for r in report["requirements"])

    assert all(w["requirement_id"] == pref for w in report["what_if"])
    assert len(report["what_if"]) == 1
    entering_ids = {e["id"] for e in report["what_if"][0]["entering"]}
    assert "cand-c" in entering_ids

    near = {m["candidate_id"]: m for m in report["near_miss"]}
    assert "cand-c" in near
    assert near["cand-c"]["requirement_id"] == pref
    assert near["cand-c"]["evidence_refs"] == []
    assert "cand-a" not in near and "cand-b" not in near
    assert "cand-d" not in near  # also fails a mandatory; one preferred flip is not enough


def test_mandatory_never_relaxed(monkeypatch):
    monkeypatch.setenv("FUNNEL_SHORTLIST_N", "1")
    store = MemoryStore()
    job_id, job = _job_with_preferred(store)
    ids = [r["id"] for r in job["requirements"]]
    mandatory = [r["id"] for r in job["requirements"] if r["kind"] == "required"]
    _seed_matches(store, job_id, [
        ("cand-top", 99, {rid: "met" for rid in ids}),
        ("cand-gap", 50, {rid: ("unmet" if rid == mandatory[0] else "met") for rid in ids}),
    ])
    report = funnel_report(store, job_id)
    assert all(w["requirement_id"] not in mandatory for w in report["what_if"])
    assert all(m["requirement_id"] not in mandatory for m in report["near_miss"])


def test_aggregates_for_llm_have_no_personal_data():
    store = MemoryStore()
    job_id, _ = _job_with_preferred(store)
    ids = [r["id"] for r in store.get_job(job_id)["requirements"]]
    _seed_matches(store, job_id, [
        ("cand-secret", 88, {rid: "met" for rid in ids}),
    ])
    # Give the candidate a personal-looking name in the store row.
    store.candidates["cand-secret"]["name"] = "Jane Doe"
    store.candidates["cand-secret"]["email"] = "jane@example.com"
    report = funnel_report(store, job_id)
    payload = aggregates_for_llm(report, "We need a Python developer. Contact hiring@example.com.")
    blob = json.dumps(payload)
    assert "Jane" not in blob and "cand-secret" not in blob
    assert "jane@example.com" not in blob
    assert "cv:" not in blob and "evidence" not in blob
    assert "candidates" not in payload and "near_miss" not in payload and "shortlist" not in payload
    assert payload["requirements"] and "text" in payload["requirements"][0]


# ---- API -------------------------------------------------------------------

def make_client(tmp_path, llm=None):
    store = MemoryStore("hiring@example.com")
    fake = llm or FakeLLM({
        "FunnelRevisionOut": {"feedback": "Clarify Docker wording.", "summary": "Tighten the container skill line."},
        "DraftOut": {"post": "Revised post about Python.", "unresolved_items": []},
        "ComplianceOut": {"findings": []},
    })
    deps = Deps(llm=fake, store=store, repo=store, channels={})
    intake, _, _ = wired_intake(tmp_path, store)
    svc = Services(settings(), store, deps, build_job_post_graph(deps, memory_checkpointer()), PolicyService(store), intake)
    return TestClient(create_app(svc)), store, fake


def login(c, user="ivy"):
    return {"Authorization": "Bearer " + c.post("/auth/login", json={"username": user, "password": f"pw-{user}"}).json()["token"]}


def test_funnel_staff_only_and_candidate_403(tmp_path):
    c, store, _ = make_client(tmp_path)
    job = open_job(store)
    ivy, guest = login(c, "ivy"), login(c, "guest")
    assert c.get(f"/jobs/{job}/funnel", headers=guest).status_code == 403
    assert c.post(f"/jobs/{job}/funnel/suggest-revision", headers=guest).status_code == 403
    assert c.get(f"/jobs/{job}/funnel").status_code == 401
    ok = c.get(f"/jobs/{job}/funnel", headers=ivy)
    assert ok.status_code == 200 and ok.json()["job_id"] == job and ok.json()["low_sample"] is True


def test_funnel_job_access_hides_other_recruiters_jobs(tmp_path):
    c, store, _ = make_client(tmp_path)
    job = open_job(store, owner_id="ivy")
    other = c.post("/auth/signup", json={"name": "Samira", "email": "samira@gmail.com", "password": "long-enough"})
    hers = {"Authorization": "Bearer " + other.json()["token"]}
    assert c.get(f"/jobs/{job}/funnel", headers=hers).status_code == 404
    assert c.get(f"/jobs/{job}/funnel", headers=login(c, "ada")).status_code == 200


def test_suggest_revision_prompt_excludes_personal_data(tmp_path):
    c, store, fake = make_client(tmp_path)
    job_id, job = _job_with_preferred(store)
    ids = [r["id"] for r in job["requirements"]]
    _seed_matches(store, job_id, [("cand-x", 70, {rid: "met" for rid in ids})])
    store.candidates["cand-x"]["name"] = "Nadia Karim"
    h = login(c, "ivy")
    r = c.post(f"/jobs/{job_id}/funnel/suggest-revision", headers=h)
    assert r.status_code == 200
    assert fake.users, "LLM should have been called"
    prompt = fake.users[-1]
    assert "Nadia" not in prompt and "cand-x" not in prompt
    assert "funnel_suggest" in {e["action"] for e in store.list_audit()}


def test_suggest_revision_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNNEL_SUGGEST_MAX_PER_DAY", "3")
    # settings() is built at client create; pass via Settings
    store = MemoryStore("hiring@example.com")
    fake = FakeLLM({"FunnelRevisionOut": {"feedback": "x", "summary": "y"}})
    deps = Deps(llm=fake, store=store, repo=store, channels={})
    intake, _, _ = wired_intake(tmp_path, store)
    svc = Services(settings(funnel_suggest_max_per_day=3), store, deps,
                   build_job_post_graph(deps, memory_checkpointer()), PolicyService(store), intake)
    c = TestClient(create_app(svc))
    job = open_job(store)
    h = login(c, "ivy")
    for _ in range(3):
        assert c.post(f"/jobs/{job}/funnel/suggest-revision", headers=h).status_code == 200
    denied = c.post(f"/jobs/{job}/funnel/suggest-revision", headers=h)
    assert denied.status_code == 429
