"""Co-pilot limits, role separation, notes, transcript JSON and the evaluation cap."""
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.core.auth import hash_password
from app.core.config import Settings
from app.graphs import Deps, build_job_post_graph, memory_checkpointer
from app.graphs.fakes import FakeLLM
from app.main import Services, create_app
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService

USERS = {"ivy": {"hash": hash_password("pw-ivy"), "role": "interviewer", "name": "Ivy", "email": "ivy@example.com"}}


def _evaluation_out(user: str):
    refs = re.findall(r"tr:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+", user)
    return {"relevance": 8, "accuracy": 7, "completeness": 7, "quality": 8,
            "justification": "The answer matches the question.", "evidence_refs": refs[:1]}


def _findings_out(user: str):
    known = json.loads(user).get("known_refs") or []
    cited = known[:1] or ["req:R1"]
    return {"strengths": [{"statement": "Gave a specific answer", "evidence_refs": cited}],
            "evidence_gaps": [{"statement": "One requirement was not discussed", "evidence_refs": [known[-1]] if known else cited}]}


def _copilot(_user: str):
    return {"questions": [
        {"question": "How have you used Python on a real service?", "requirement_id": "R1", "reference_answer": "A service they built."},
        {"question": "Where did you use Docker?", "requirement_id": "R2", "reference_answer": "An image they shipped."},
    ]}


def _client(**settings):
    store = MemoryStore()
    store.create_job("job-1", "ivy")
    store.update_job("job-1", title="Backend Developer", profile={
        "title": "Backend Developer", "required_skills": ["Python", "Docker"], "contact_email": "hiring@example.com",
    })
    store.update_job("job-1", status="open")
    store.create_candidate({"id": "cand-1", "job_id": "job-1", "email": "ada@example.com", "source": "email", "name": "Ada", "status": "screened"})
    store.save_cv_result("cand-1", "job-1", {
        "status": "completed", "aggregate_score": 76.0,
        "matches": [
            {"requirement_id": "R1", "status": "met", "justification": "Python work", "evidence_refs": ["cv:cand-1:c001"]},
            {"requirement_id": "R2", "status": "unmet", "justification": "Docker was not shown", "evidence_refs": []},
        ],
        "dimension_scores": [
            {"dimension": "technical_skills", "score": 80, "justification": "Strong match", "evidence_refs": ["cv:cand-1:c001"]},
        ],
    })
    llm = FakeLLM({"CopilotOut": _copilot, "EvaluationOut": _evaluation_out, "FindingsOut": _findings_out})
    deps = Deps(llm=llm, store=store, repo=store, channels={}, apply_email="hiring@example.com")
    config = Settings(auth_secret="test-secret", users_json=json.dumps(USERS), public_base_url="http://localhost:5173",
                      imap_host="imap.gmail.com", imap_user="hiring@example.com", imap_password="app-password", **settings)
    svc = Services(config, store, deps, build_job_post_graph(deps, memory_checkpointer()), PolicyService(store))
    client = TestClient(create_app(svc))
    token = client.post("/auth/login", json={"username": "ivy", "password": "pw-ivy"}).json()["token"]
    return client, {"Authorization": f"Bearer {token}"}, store


def _schedule(client, headers, monkeypatch):
    monkeypatch.setattr("app.api.interviews.send_invite", lambda *_a, **_k: None)
    starts = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    response = client.post("/interviews", headers=headers, json={"job_id": "job-1", "candidate_id": "cand-1", "starts_at": starts})
    assert response.status_code == 200, response.text
    body = response.json()
    joined = client.post(f"/join/{body['join_token']}", json={"consent": True})
    assert joined.status_code == 200, joined.text
    return body, joined.json()["token"]


def test_copilot_limits_return_429(monkeypatch):
    client, headers, _store = _client(copilot_max_calls=2, copilot_per_minute=1, copilot_daily_tokens=100_000)
    body, _token = _schedule(client, headers, monkeypatch)
    first = client.post(f"/interviews/{body['id']}/copilot", headers=headers, json={"message": "What should I ask next?"})
    assert first.status_code == 200, first.text
    assert first.json()["remaining"] == 1 and first.json()["limit"] == 2
    assert len(first.json()["questions"]) >= 2
    minute = client.post(f"/interviews/{body['id']}/copilot", headers=headers, json={"message": "Another question"})
    assert minute.status_code == 429
    assert "minute" in minute.json()["detail"].lower()

    client, headers, _store = _client(copilot_max_calls=1, copilot_per_minute=10)
    body, _token = _schedule(client, headers, monkeypatch)
    assert client.post(f"/interviews/{body['id']}/copilot", headers=headers, json={"message": "What next?"}).status_code == 200
    blocked = client.post(f"/interviews/{body['id']}/copilot", headers=headers, json={"message": "And now?"})
    assert blocked.status_code == 429
    assert "0 of 1" in blocked.json()["detail"]

    client, headers, store = _client(copilot_daily_tokens=1)
    body, _token = _schedule(client, headers, monkeypatch)
    budget = client.post(f"/interviews/{body['id']}/copilot", headers=headers, json={"message": "What next?"})
    assert budget.status_code == 429
    assert store.copilot_remaining(body["id"], 15) == 15


def test_a_candidate_cannot_open_staff_tools(monkeypatch):
    client, headers, _store = _client()
    body, candidate = _schedule(client, headers, monkeypatch)
    candidate_headers = {"Authorization": f"Bearer {candidate}"}
    paths = [
        ("GET", f"/interviews/{body['id']}/brief"),
        ("GET", f"/interviews/{body['id']}/copilot"),
        ("POST", f"/interviews/{body['id']}/copilot"),
        ("GET", f"/interviews/{body['id']}/notes"),
        ("PUT", f"/interviews/{body['id']}/notes"),
        ("GET", f"/interviews/{body['id']}/transcript"),
        ("PATCH", f"/interviews/{body['id']}/transcript/turns/t0001"),
        ("POST", f"/interviews/{body['id']}/evaluate"),
    ]
    for method, path in paths:
        response = client.request(method, path, headers=candidate_headers, json={"message": "hi", "body": "note"})
        assert response.status_code == 403, (method, path, response.status_code, response.text)


def test_notes_are_saved_on_the_candidate(monkeypatch):
    client, headers, store = _client()
    body, _token = _schedule(client, headers, monkeypatch)
    saved = client.put(f"/interviews/{body['id']}/notes", headers=headers, json={"body": "Ask about testing.", "mark": True})
    assert saved.status_code == 200, saved.text
    note = saved.json()
    assert note["body"] == "Ask about testing."
    assert note["author_name"] == "Ivy"
    assert note["markers"][0]["offset_ms"] >= 0
    again = client.get(f"/interviews/{body['id']}/notes", headers=headers)
    assert again.json()["body"] == "Ask about testing."
    profile = client.get("/candidates/cand-1", headers=headers).json()
    assert profile["notes"][0]["body"] == "Ask about testing."
    too_long = client.put(f"/interviews/{body['id']}/notes", headers=headers, json={"body": "x" * 5001})
    assert too_long.status_code == 422
    store.delete_candidate("cand-1")
    assert store.get_note(body["id"]) is None
    assert store.get_interview(body["id"]) is None


def test_speech_becomes_one_transcript_document(monkeypatch, tmp_path):
    client, headers, store = _client(transcript_dir=str(tmp_path))
    body, candidate = _schedule(client, headers, monkeypatch)
    staff = headers["Authorization"].removeprefix("Bearer ")
    url = f"/ws/interview/{body['id']}"
    with client.websocket_connect(url) as interviewer, client.websocket_connect(url) as person:
        interviewer.send_json({"type": "auth", "token": staff})
        person.send_json({"type": "auth", "token": candidate})
        assert interviewer.receive_json()["state"] == "joined"
        assert person.receive_json()["state"] == "joined"
        interviewer.send_json({"type": "speech", "text": "Tell me about Python."})
        first = interviewer.receive_json()
        assert first["type"] == "transcript" and first["turn"]["speaker"] == "interviewer"
        person.send_json({"type": "speech", "text": "I built services with Python."})
        second = interviewer.receive_json()
        assert second["turn"]["speaker"] == "candidate"
        # Candidate isolation is covered in test_stt; Starlette receive_json has no timeout
        # (a blocking receive here would hang forever when nothing is sent).
    ended = client.post(f"/interviews/{body['id']}/end", headers=headers)
    assert ended.status_code == 200, ended.text
    doc = client.get(f"/interviews/{body['id']}/transcript", headers=headers).json()
    assert set(doc) >= {"interview_id", "candidate_id", "job_id", "started_at", "ended_at", "turns"}
    assert doc["interview_id"] == body["id"] and doc["candidate_id"] == "cand-1"
    assert [t["speaker"] for t in doc["turns"]] == ["interviewer", "candidate"]
    assert doc["turns"][0]["turn_id"] == "t0001"
    assert doc["turns"][0]["text"] == "Tell me about Python."
    assert doc["turns"][0]["end_ms"] >= doc["turns"][0]["start_ms"]
    saved = json.loads((tmp_path / f"{body['id']}.json").read_text(encoding="utf-8"))
    assert saved["turns"][1]["text"] == "I built services with Python."
    store.delete_candidate("cand-1")
    assert not (tmp_path / f"{body['id']}.json").exists()


def test_evaluation_stops_after_two_runs(monkeypatch):
    client, headers, _store = _client(eval_max_runs=2)
    body, candidate = _schedule(client, headers, monkeypatch)
    staff = headers["Authorization"].removeprefix("Bearer ")
    url = f"/ws/interview/{body['id']}"
    with client.websocket_connect(url) as interviewer, client.websocket_connect(url) as person:
        interviewer.send_json({"type": "auth", "token": staff})
        person.send_json({"type": "auth", "token": candidate})
        interviewer.receive_json()
        person.receive_json()
        interviewer.send_json({"type": "speech", "text": "How do you use Python?"})
        person.send_json({"type": "speech", "text": "I have used Python for three years."})
    ended = client.post(f"/interviews/{body['id']}/end", headers=headers)
    assert ended.status_code == 200
    # End auto-runs Gemini once; profile should already have an assessment.
    profile = client.get("/candidates/cand-1", headers=headers).json()
    assert profile["evaluation"], profile
    assert profile["evaluation"]["qa_evaluations"]
    assert profile["evaluation"]["qa_evaluations"][0]["relevance"] == 8
    assert "uncovered_requirement_ids" in profile["evaluation"]
    assert profile["status"] == "assessed"
    report_id = profile["evaluation"]["id"]
    # One manual re-run remains under the default cap of two.
    second = client.post(f"/interviews/{body['id']}/assessment", headers=headers)
    assert second.status_code == 200, second.text
    third = client.post(f"/interviews/{body['id']}/evaluate", headers=headers)
    assert third.status_code == 429
    again = client.get("/candidates/cand-1", headers=headers).json()
    assert again["evaluation"]["id"] == report_id or again["evaluation"]["id"] == second.json()["id"]
