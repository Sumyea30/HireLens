"""Scheduling emails the candidate the room link and a Google Calendar link."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.auth import hash_password, issue_token
from app.core.config import Settings
from app.graphs import Deps, build_job_post_graph, memory_checkpointer
from app.graphs.fakes import FakeLLM
from app.main import Services, create_app
from app.services.invite import JOIN_EARLY_MINUTES, JOIN_LATE_MINUTES, _message, build_invite, invite_parts, join_blocked
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService

USERS = {"ivy": {"hash": hash_password("pw-ivy"), "role": "interviewer", "name": "Ivy"}}


def _client():
    store = MemoryStore()
    store.create_job("job-1", "ivy")
    store.update_job(
        "job-1", title="Backend Developer", status="open",
        profile={"title": "Backend Developer", "company": "Acme Hiring", "contact_email": "hiring@example.com",
                 "required_skills": ["Python"], "min_experience_years": 2, "qualifications": ["BSc"]},
    )
    store.create_candidate({
        "id": "cand-1", "job_id": "job-1", "email": "ada@example.com", "source": "email", "name": "Ada",
        "status": "screened", "received_at": "2026-10-01T09:00:00+00:00",
    })
    store.create_candidate({"id": "cand-2", "job_id": "job-1", "email": None, "source": "upload", "name": "No Mail", "status": "screened"})
    deps = Deps(llm=FakeLLM({}), store=store, repo=store, channels={}, apply_email="hiring@example.com")
    settings = Settings(auth_secret="test-secret", users_json=json.dumps(USERS), public_base_url="http://localhost:5173",
                        imap_host="imap.gmail.com", imap_user="hiring@example.com", imap_password="app-password")
    svc = Services(settings, store, deps, build_job_post_graph(deps, memory_checkpointer()), PolicyService(store))
    client = TestClient(create_app(svc))
    token = client.post("/auth/login", json={"username": "ivy", "password": "pw-ivy"}).json()["token"]
    return client, {"Authorization": f"Bearer {token}"}, store


def _soon() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()


def test_the_invite_contains_the_room_and_calendar_links():
    start = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    subject, body = build_invite(
        job_title="Backend Developer", starts_at=start, room_url="http://localhost:5173/join/abc",
        job_id="job-1", applied_at="2026-10-01T09:00:00+00:00", company_name="Acme Hiring",
        recruiter_email="ivy@example.com",
    )
    assert "Congratulations" in subject
    assert "next-round interview for Backend Developer" in subject
    assert "job job-1 that you applied to on 01 October 2026" in body
    assert "selected for the next round" in body
    assert "HR / Company: Acme Hiring" in body
    assert "Recruiter: ivy@example.com" in body
    assert f"opens {JOIN_EARLY_MINUTES} minutes" in body
    assert f"up to {JOIN_LATE_MINUTES} minutes" in body
    assert "http://localhost:5173/join/abc" in body
    assert "https://calendar.google.com/calendar/render?" in body
    assert "20261101T100000Z" in body


def test_join_window_blocks_early_and_late():
    start = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    assert join_blocked(start, now=start - timedelta(minutes=JOIN_EARLY_MINUTES + 1))
    assert join_blocked(start, now=start - timedelta(minutes=JOIN_EARLY_MINUTES)) is None
    assert join_blocked(start, now=start + timedelta(minutes=JOIN_LATE_MINUTES)) is None
    assert join_blocked(start, now=start + timedelta(minutes=JOIN_LATE_MINUTES + 1))
    assert join_blocked(start, status="live", now=start + timedelta(hours=2)) is None
    assert join_blocked(start, status="completed", now=start)


def test_scheduling_emails_the_application_address(monkeypatch):
    sent = []

    def fake_send(_settings, to, subject, body, **kwargs):
        sent.append({"to": to, "subject": subject, "body": body, **kwargs})

    monkeypatch.setattr("app.api.interviews.send_invite", fake_send)
    client, headers, store = _client()
    starts = _soon()
    response = client.post("/interviews", headers=headers, json={"job_id": "job-1", "candidate_id": "cand-1", "starts_at": starts})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email_sent"] is True and body["join_token"]
    assert sent[0]["to"] == "ada@example.com"
    assert "Congratulations" in sent[0]["subject"]
    assert "selected for the next round" in sent[0]["body"]
    assert "job-1" in sent[0]["body"] and "01 October 2026" in sent[0]["body"]
    assert "Acme Hiring" in sent[0]["body"]
    assert f"http://localhost:5173/join/{body['join_token']}" in sent[0]["body"]
    assert "calendar.google.com" in sent[0]["body"]
    assert "score" not in sent[0]["body"].lower()
    assert store.get_candidate("cand-1")["status"] == "interview_scheduled"
    assert "join_token_hash" not in client.get("/interviews", headers=headers).json()[0]

    missing = client.post("/interviews", headers=headers, json={"job_id": "job-1", "candidate_id": "cand-2", "starts_at": starts})
    assert missing.status_code == 422

    early = client.post("/interviews", headers=headers, json={
        "job_id": "job-1", "candidate_id": "cand-1",
        "starts_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    })
    assert early.status_code == 200
    too_soon = client.post(f"/join/{early.json()['join_token']}", json={"consent": True})
    assert too_soon.status_code == 403
    assert "opens" in too_soon.json()["detail"].lower()

    joined = client.post(f"/join/{body['join_token']}", json={"consent": True})
    assert joined.status_code == 200 and joined.json()["interview_id"] == body["id"]

    url = f"/ws/interview/{body['id']}"
    with client.websocket_connect(url) as interviewer, client.websocket_connect(url) as candidate:
        interviewer.send_json({"type": "auth", "token": headers["Authorization"].removeprefix("Bearer ")})
        candidate.send_json({"type": "auth", "token": joined.json()["token"]})
        assert interviewer.receive_json() == {"type": "peer", "state": "joined"}
        assert candidate.receive_json() == {"type": "peer", "state": "joined"}
        candidate.send_json({"type": "ready"})
        assert interviewer.receive_json() == {"type": "peer", "state": "ready"}
        interviewer.send_json({"type": "signal", "kind": "offer", "data": {"type": "offer", "sdp": "v=0"}})
        offer = candidate.receive_json()
        assert offer["type"] == "signal" and offer["kind"] == "offer" and offer["data"]["sdp"] == "v=0"
        candidate.send_json({"type": "signal", "kind": "answer", "data": {"type": "answer", "sdp": "v=answer"}})
        answer = interviewer.receive_json()
        assert answer == {"type": "signal", "kind": "answer", "data": {"type": "answer", "sdp": "v=answer"}}
        interviewer.send_json({"type": "signal", "kind": "ice", "data": {"candidate": "a=candidate:1", "sdpMid": "0"}})
        ice = candidate.receive_json()
        assert ice["type"] == "signal" and ice["kind"] == "ice" and ice["data"]["candidate"] == "a=candidate:1"


def _scheduled(client, headers):
    response = client.post("/interviews", headers=headers, json={"job_id": "job-1", "candidate_id": "cand-1", "starts_at": _soon()})
    assert response.status_code == 200, response.text
    body = response.json()
    joined = client.post(f"/join/{body['join_token']}", json={"consent": True})
    assert joined.status_code == 200, joined.text
    return body["id"], joined.json()["token"]


def test_leaving_tells_the_other_person(monkeypatch):
    monkeypatch.setattr("app.api.interviews.send_invite", lambda *_a, **_k: None)
    client, headers, _store = _client()
    interview_id, candidate_token = _scheduled(client, headers)
    staff = headers["Authorization"].removeprefix("Bearer ")
    url = f"/ws/interview/{interview_id}"
    with client.websocket_connect(url) as interviewer:
        interviewer.send_json({"type": "auth", "token": staff})
        with client.websocket_connect(url) as candidate:
            candidate.send_json({"type": "auth", "token": candidate_token})
            assert interviewer.receive_json() == {"type": "peer", "state": "joined"}
            assert candidate.receive_json() == {"type": "peer", "state": "joined"}
        assert interviewer.receive_json() == {"type": "peer", "state": "left"}


def test_a_second_connection_from_the_same_person_replaces_the_first(monkeypatch):
    monkeypatch.setattr("app.api.interviews.send_invite", lambda *_a, **_k: None)
    client, headers, _store = _client()
    interview_id, candidate_token = _scheduled(client, headers)
    staff = headers["Authorization"].removeprefix("Bearer ")
    url = f"/ws/interview/{interview_id}"
    with client.websocket_connect(url) as first, client.websocket_connect(url) as second:
        first.send_json({"type": "auth", "token": staff})
        second.send_json({"type": "auth", "token": staff})
        with pytest.raises(WebSocketDisconnect) as caught:
            first.receive_json()
        assert caught.value.code == 4000
        with client.websocket_connect(url) as candidate:
            candidate.send_json({"type": "auth", "token": candidate_token})
            assert second.receive_json() == {"type": "peer", "state": "joined"}
            assert candidate.receive_json() == {"type": "peer", "state": "joined"}


def test_a_third_person_cannot_join_the_call(monkeypatch):
    monkeypatch.setattr("app.api.interviews.send_invite", lambda *_a, **_k: None)
    client, headers, _store = _client()
    interview_id, candidate_token = _scheduled(client, headers)
    staff = headers["Authorization"].removeprefix("Bearer ")
    admin = issue_token("test-secret", "admin-1", "Admin", "admin", 30)
    url = f"/ws/interview/{interview_id}"
    with client.websocket_connect(url) as interviewer, client.websocket_connect(url) as candidate:
        interviewer.send_json({"type": "auth", "token": staff})
        candidate.send_json({"type": "auth", "token": candidate_token})
        assert interviewer.receive_json()["state"] == "joined"
        assert candidate.receive_json()["state"] == "joined"
        with client.websocket_connect(url) as extra:
            extra.send_json({"type": "auth", "token": admin})
            with pytest.raises(WebSocketDisconnect) as caught:
                extra.receive_json()
            assert caught.value.code == 4403


def test_the_calendar_file_is_a_request_with_a_reminder():
    start = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    parts = invite_parts(
        job_title="Backend Developer", starts_at=start, room_url="http://localhost:5173/join/abc",
        uid="int-1@hirelens", sequence=2, organizer="hiring@example.com", attendee="ada@example.com",
        job_id="job-1", applied_at="2026-10-01T09:00:00+00:00", company_name="Acme Hiring",
        recruiter_email="ivy@example.com",
    )
    ics = parts.ics
    assert "METHOD:REQUEST" in ics
    assert "UID:int-1@hirelens" in ics
    assert "SEQUENCE:2" in ics
    assert "DTSTAMP:" in ics
    assert "DTSTART:20261101T100000Z" in ics
    assert "DTEND:20261101T110000Z" in ics
    assert "ORGANIZER:mailto:hiring@example.com" in ics
    assert "ATTENDEE" in ics and "mailto:ada@example.com" in ics
    assert "LOCATION:http://localhost:5173/join/abc" in ics
    assert "DESCRIPTION:Join the HireLens interview room: http://localhost:5173/join/abc" in ics
    assert "TRIGGER:-PT15M" in ics
    assert "Congratulations" in parts.plain and "Acme Hiring" in parts.plain
    assert "score" not in parts.plain.lower() and "score" not in parts.html.lower()
    raw = _message("hiring@example.com", "ada@example.com", parts.subject, parts.plain, parts.html, parts.ics).as_string()
    assert raw.lower().count("text/calendar") >= 2
    assert "method=REQUEST" in raw or "method=\"REQUEST\"" in raw.replace("\n", "")
    assert "invite.ics" in raw


def test_both_people_are_emailed_and_reschedule_bumps_the_sequence(monkeypatch):
    sent = []

    def fake_send(_settings, to, subject, body, **kwargs):
        sent.append({"to": to, "body": body, "ics": kwargs.get("ics") or "", "html": kwargs.get("html") or ""})

    monkeypatch.setattr("app.api.interviews.send_invite", fake_send)
    client, headers, _store = _client()
    interviewer = dict(USERS["ivy"], email="ivy@example.com")
    client.app.state.services.settings = client.app.state.services.settings.__class__(
        **{**client.app.state.services.settings.__dict__, "users_json": json.dumps({"ivy": interviewer})})
    starts = _soon()
    created = client.post("/interviews", headers=headers, json={"job_id": "job-1", "candidate_id": "cand-1", "starts_at": starts})
    assert created.status_code == 200, created.text
    body = created.json()
    assert {row["to"] for row in sent} == {"ada@example.com", "ivy@example.com"}
    candidate = next(row for row in sent if row["to"] == "ada@example.com")
    staff = next(row for row in sent if row["to"] == "ivy@example.com")
    assert "Congratulations" in candidate["body"] and "selected for the next round" in candidate["body"]
    assert "Recruiter: ivy@example.com" in candidate["body"]
    assert "You are scheduled to interview a candidate" in staff["body"]
    assert f"/join/{body['join_token']}" in candidate["body"]
    assert f"/interviews/{body['id']}/room" in staff["body"]
    assert f"/interviews/{body['id']}/room" not in candidate["body"]
    assert "METHOD:REQUEST" in candidate["ics"]
    assert "SEQUENCE:0" in candidate["ics"] and "SEQUENCE:0" in staff["ics"]
    assert "UID:" in candidate["ics"] and candidate["ics"].split("UID:")[1].split("\r\n")[0] == staff["ics"].split("UID:")[1].split("\r\n")[0]
    assert "<p>" in candidate["html"]
    sent.clear()
    later = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    moved = client.post(f"/interviews/{body['id']}/reschedule", headers=headers, json={"starts_at": later})
    assert moved.status_code == 200, moved.text
    assert "SEQUENCE:1" in sent[0]["ics"]
    assert sent[0]["ics"].split("UID:")[1].split("\r\n")[0] == candidate["ics"].split("UID:")[1].split("\r\n")[0]
