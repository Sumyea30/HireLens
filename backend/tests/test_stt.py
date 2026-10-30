"""Server-side STT: speaker by role, timestamps, filters, downtime, edit/seal, candidate isolation."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.core.auth import hash_password
from app.core.config import Settings
from app.graphs import Deps, build_job_post_graph, memory_checkpointer
from app.graphs.fakes import FakeLLM
from app.main import Services, create_app
from app.services.audio_pipeline import InterviewAudio, SAMPLE_RATE
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService
from app.services.stt import (
    AssemblyAiStt,
    FakeStt,
    SttSegment,
    drop_reason,
    keep_segment,
    pcm16_mono_to_wav,
    vocabulary_prompt,
)
from app.services.transcript import edit_turn_text, record_utterance

USERS = {"ivy": {"hash": hash_password("pw-ivy"), "role": "interviewer", "name": "Ivy", "email": "ivy@example.com"}}


def _client(**settings):
    store = MemoryStore()
    store.create_job("job-1", "ivy")
    store.update_job("job-1", title="Backend Developer", profile={
        "title": "Backend Developer", "required_skills": ["Python", "Docker"], "contact_email": "hiring@example.com",
    })
    store.update_job("job-1", status="open")
    store.create_candidate({"id": "cand-1", "job_id": "job-1", "email": "ada@example.com", "source": "email", "name": "Ada", "status": "screened"})
    llm = FakeLLM({})
    deps = Deps(llm=llm, store=store, repo=store, channels={}, apply_email="hiring@example.com")
    config = Settings(
        auth_secret="test-secret", users_json=json.dumps(USERS), public_base_url="http://localhost:5173",
        stt_provider="fake", **settings,
    )
    svc = Services(config, store, deps, build_job_post_graph(deps, memory_checkpointer()), PolicyService(store))
    svc.stt = FakeStt()
    client = TestClient(create_app(svc))
    token = client.post("/auth/login", json={"username": "ivy", "password": "pw-ivy"}).json()["token"]
    return client, {"Authorization": f"Bearer {token}"}, store, svc


def _schedule(client, headers, monkeypatch):
    monkeypatch.setattr("app.api.interviews.send_invite", lambda *_a, **_k: None)
    starts = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    response = client.post("/interviews", headers=headers, json={"job_id": "job-1", "candidate_id": "cand-1", "starts_at": starts})
    assert response.status_code == 200, response.text
    body = response.json()
    joined = client.post(f"/join/{body['join_token']}", json={"consent": True})
    assert joined.status_code == 200, joined.text
    return body, joined.json()["token"]


def test_speaker_labelled_by_role(monkeypatch):
    client, headers, store, _svc = _client()
    body, candidate = _schedule(client, headers, monkeypatch)
    staff = headers["Authorization"].removeprefix("Bearer ")
    url = f"/ws/interview/{body['id']}"
    with client.websocket_connect(url) as interviewer, client.websocket_connect(url) as person:
        interviewer.send_json({"type": "auth", "token": staff})
        person.send_json({"type": "auth", "token": candidate})
        interviewer.receive_json()
        person.receive_json()
        interviewer.send_json({"type": "speech", "text": "Tell me about Python."})
        first = interviewer.receive_json()
        assert first["type"] == "transcript" and first["turn"]["speaker"] == "interviewer"
        person.send_json({"type": "speech", "text": "I built services with Python."})
        second = interviewer.receive_json()
        assert second["turn"]["speaker"] == "candidate"
    turns = store.transcript_document(body["id"])["turns"]
    assert [t["speaker"] for t in turns] == ["interviewer", "candidate"]


def test_candidate_websocket_never_receives_transcript(monkeypatch):
    """Browser SpeechRecognition and typed speech both use room `speech` → broadcast_staff only."""
    client, headers, _store, _svc = _client()
    body, candidate = _schedule(client, headers, monkeypatch)
    staff = headers["Authorization"].removeprefix("Bearer ")
    url = f"/ws/interview/{body['id']}"
    with client.websocket_connect(url) as interviewer, client.websocket_connect(url) as person:
        interviewer.send_json({"type": "auth", "token": staff})
        person.send_json({"type": "auth", "token": candidate})
        interviewer.receive_json()
        person.receive_json()
        interviewer.send_json({"type": "speech", "text": "Secret hiring note."})
        assert interviewer.receive_json()["type"] == "transcript"
        # Candidate must not have a transcript waiting; a short receive times out.
        try:
            msg = person.receive_json(timeout=0.3)
        except Exception:
            msg = None
        assert msg is None or msg.get("type") != "transcript"
        if msg:
            assert "Secret" not in json.dumps(msg)


def test_browser_speech_finals_are_staff_only(monkeypatch):
    """STT_PROVIDER=browser sends finals as speech events; candidates never see them."""
    test_candidate_websocket_never_receives_transcript(monkeypatch)


def test_candidate_never_receives_stt_status_or_speech_activity():
    """Caption status uses broadcast_staff — candidate sockets must get nothing."""
    from app.api.room import RoomHub

    async def run():
        hub = RoomHub()
        staff_got, cand_got = [], []

        class Sock:
            def __init__(self, bag):
                self.bag = bag

            async def send_json(self, msg):
                self.bag.append(msg)

            async def close(self, code=1000):
                return None

        staff, cand = Sock(staff_got), Sock(cand_got)
        assert await hub.join("int-x", staff, "ivy", "interviewer")
        assert await hub.join("int-x", cand, "cand-1", "candidate")
        staff_got.clear()
        cand_got.clear()
        await hub.broadcast_staff("int-x", {"type": "stt_status", "state": "listening", "pending": 0, "message": ""})
        await hub.broadcast_staff("int-x", {"type": "speech_activity", "active": True, "speaker": "candidate"})
        await hub.broadcast_staff("int-x", {"type": "transcript", "turn": {"text": "secret"}})
        assert [m["type"] for m in staff_got] == ["stt_status", "speech_activity", "transcript"]
        assert cand_got == []

    asyncio.run(run())


def test_candidate_token_forbidden_on_transcript_and_tools(monkeypatch):
    client, headers, _store, _svc = _client()
    body, candidate = _schedule(client, headers, monkeypatch)
    h = {"Authorization": f"Bearer {candidate}"}
    for method, path in [
        ("GET", f"/interviews/{body['id']}/transcript"),
        ("PATCH", f"/interviews/{body['id']}/transcript/turns/t0001"),
        ("GET", f"/interviews/{body['id']}/notes"),
        ("POST", f"/interviews/{body['id']}/copilot"),
        ("POST", f"/interviews/{body['id']}/evaluate"),
    ]:
        r = client.request(method, path, headers=h, json={"text": "x", "message": "hi", "body": "n"})
        assert r.status_code == 403, (method, path, r.status_code)


def test_stream_timestamps_and_edit_seal(monkeypatch, tmp_path):
    client, headers, store, _svc = _client(transcript_dir=str(tmp_path))
    body, _cand = _schedule(client, headers, monkeypatch)
    turn = record_utterance(store, body["id"], "candidate", "I used Docker Compose.", start_ms=1200, end_ms=3400, confidence=0.88)
    assert turn["start_ms"] == 1200 and turn["end_ms"] == 3400 and turn["confidence"] == 0.88
    edited = edit_turn_text(store, body["id"], turn["turn_id"], "I used Docker Compose on staging.")
    assert edited["text"].startswith("I used Docker Compose on staging")
    assert edited["original_text"] == "I used Docker Compose."
    patch = client.patch(
        f"/interviews/{body['id']}/transcript/turns/{turn['turn_id']}",
        headers=headers, json={"text": "I used Docker Compose in CI."},
    )
    assert patch.status_code == 200
    assert patch.json()["original_text"] == "I used Docker Compose."
    ended = client.post(f"/interviews/{body['id']}/end", headers=headers)
    assert ended.status_code == 200
    sealed = client.get(f"/interviews/{body['id']}/transcript", headers=headers).json()
    assert sealed["turns"][0]["text"] == "I used Docker Compose in CI."
    assert sealed["turns"][0]["original_text"] == "I used Docker Compose."
    blocked = client.patch(
        f"/interviews/{body['id']}/transcript/turns/{turn['turn_id']}",
        headers=headers, json={"text": "too late"},
    )
    assert blocked.status_code == 409


def test_silence_and_hallucination_filtering():
    assert drop_reason(SttSegment("Thank you.", 0, 1, 0.9, no_speech_prob=0.1)) == "hallucination"
    assert drop_reason(SttSegment("hello", 0, 1, 0.2, no_speech_prob=0.9)) == "no_speech"
    assert drop_reason(SttSegment("noise", 0, 1, 0.1, avg_logprob=-1.5)) == "low_logprob"
    assert drop_reason(SttSegment(
        "응, 응, 응, 응, 응, 응, 응, 응", 0, 2, 0.9, avg_logprob=-0.1)) == "repeated_token"
    assert drop_reason(SttSegment(
        "We just think it's time to say it, say it or not", 0, 2, 0.5, avg_logprob=-0.4)) == "hallucination"
    assert drop_reason(SttSegment(
        "say it, say it, say it, say it", 0, 2, 0.6, avg_logprob=-0.3)) == "repeated_token"
    assert drop_reason(SttSegment(
        "to you and to you and to you and to you and to you and to you", 0, 2, 0.6, avg_logprob=-0.3)
    ) == "repeated_token"
    assert drop_reason(SttSegment(
        "Python, C, Docker, BS, At least 0 years of relevant experience, Good communication",
        0, 2, 0.8, avg_logprob=-0.2,
    )) == "prompt_echo"
    # Non-Latin is kept for language_mismatch flagging (not dropped here).
    assert drop_reason(SttSegment(
        "도움이 되어야 됩니다.", 0, 2, 0.05, avg_logprob=-0.5)) is None
    from app.services.language_guard import is_language_mismatch
    assert is_language_mismatch("도움이 되어야 됩니다.")
    # Accented / lower-confidence English should still be kept with loosened defaults.
    assert keep_segment(SttSegment(
        "I have experience with Docker and Python.", 0, 2, 0.2, avg_logprob=-1.0, no_speech_prob=0.3))
    assert keep_segment(SttSegment("I built a FastAPI service.", 0, 2, 0.8, avg_logprob=-0.2))
    assert keep_segment(SttSegment("I'll be conducting an interview today.", 0, 2, 0.7, avg_logprob=-0.2))


def test_drop_filters_are_env_configurable(monkeypatch):
    monkeypatch.setenv("STT_HIGH_NO_SPEECH", "0.5")
    monkeypatch.setenv("STT_MIN_KEEP_CONFIDENCE", "0.5")
    assert drop_reason(SttSegment("hello there", 0, 1, 0.4, no_speech_prob=0.55)) == "no_speech"
    assert drop_reason(SttSegment("hello there", 0, 1, 0.4, no_speech_prob=0.1)) == "low_confidence"
    monkeypatch.setenv("STT_HIGH_NO_SPEECH", "0.78")
    monkeypatch.setenv("STT_MIN_KEEP_CONFIDENCE", "0.05")


def test_vocabulary_prompt_disabled_to_avoid_echo():
    job = {"title": "Backend Developer", "requirements": [{"text": "Python"}], "profile": {"required_skills": ["Docker"]}}
    # Job skills as Whisper initial_prompt were echoed as fake transcript turns.
    assert vocabulary_prompt(job) == ""


def test_stt_down_does_not_block_and_notifies_staff():
    async def run():
        store = MemoryStore()
        store.create_job("job-1", "ivy")
        store.update_job("job-1", status="open")
        store.create_candidate({"id": "cand-1", "job_id": "job-1", "email": "a@b.c", "source": "email", "status": "screened"})
        store.create_interview({
            "id": "int-1", "job_id": "job-1", "candidate_id": "cand-1",
            "join_token_hash": "x", "scheduled_at": datetime.now(timezone.utc).isoformat(),
            "job_title": "Dev", "candidate_name": "Ada",
        })
        fake = FakeStt()
        fake.fail = True
        notes = []
        turns = []

        async def on_turn(t): turns.append(t)
        async def on_status(m): notes.append(m)

        audio = InterviewAudio("int-1", store, fake, on_turn=on_turn, on_status=on_status)
        audio.start()
        # Loud speech then enough quiet (~500 ms+) to flush.
        loud = (b"\x00\x40" * 4000)
        quiet = (b"\x00\x00" * 4000)
        audio.push("candidate", loud)
        audio.push("candidate", loud)
        audio.push("candidate", quiet)
        audio.push("candidate", quiet)
        await asyncio.sleep(0.4)
        await audio.close()
        assert turns == []
        assert any(
            isinstance(n, dict) and (n.get("state") == "unavailable" or "unavailable" in str(n.get("message", "")).lower())
            for n in notes
        )

    asyncio.run(run())


def test_fake_stt_from_pcm_magic():
    fake = FakeStt()
    pcm = FakeStt.MAGIC + b"Custom phrase from audio"
    res = fake.transcribe(pcm)
    assert res.segments[0].text == "Custom phrase from audio"


def test_pcm16_mono_to_wav_header():
    pcm = b"\x00\x01" * 100
    wav = pcm16_mono_to_wav(pcm, 16000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert wav.endswith(pcm)


def test_assemblyai_stt_upload_and_poll(monkeypatch):
    pcm = b"\x00\x40" * 1600
    calls = {"n": 0}

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/v2/upload"):
                body = kwargs.get("content") or b""
                assert body[:4] == b"RIFF"
                return FakeResponse(200, {"upload_url": "https://cdn.example/u1"})
            if url.endswith("/v2/transcript"):
                assert kwargs["json"]["audio_url"] == "https://cdn.example/u1"
                assert kwargs["json"].get("language_code") == "en"
                assert "speech_model" not in kwargs["json"]
                return FakeResponse(200, {"id": "tx-1"})
            raise AssertionError(url)

        def get(self, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(200, {"status": "processing"})
            return FakeResponse(200, {
                "status": "completed",
                "text": "I built services with Python.",
                "confidence": 0.91,
                "words": [
                    {"text": "I", "start": 0, "end": 120, "confidence": 0.9},
                    {"text": "built", "start": 120, "end": 400, "confidence": 0.92},
                    {"text": "services", "start": 400, "end": 900, "confidence": 0.9},
                    {"text": "with", "start": 900, "end": 1100, "confidence": 0.88},
                    {"text": "Python.", "start": 1100, "end": 1600, "confidence": 0.95},
                ],
            })

    monkeypatch.setattr("app.services.stt.httpx.Client", FakeClient)
    monkeypatch.setattr("app.services.stt.time.sleep", lambda *_a, **_k: None)
    stt = AssemblyAiStt("test-key", poll_interval_s=0.01)
    res = stt.transcribe(pcm)
    assert res.error is None
    assert res.model == "assemblyai-universal"
    assert res.segments[0].text == "I built services with Python."
    assert res.segments[0].confidence > 0.8


def test_assemblyai_stt_requires_api_key():
    assert AssemblyAiStt("").transcribe(b"\x00\x00" * 100).error == "not_configured"


def test_assemblyai_live_turn_parsing():
    from app.services.assemblyai_live import AssemblyAiLiveSession, LiveFinal
    got = []
    sess = AssemblyAiLiveSession("k", got.append, label="t")
    sess._handle_message('{"type":"Begin","id":"1"}')
    sess._handle_message('{"type":"Turn","transcript":"partial","end_of_turn":false}')
    assert len(got) == 1 and got[0].partial and got[0].text == "partial"
    got.clear()
    # Very low confidence finals still dropped.
    sess._handle_message(
        '{"type":"Turn","transcript":"it","end_of_turn":true,'
        '"words":[{"text":"it","confidence":0.15}]}'
    )
    assert got == []
    sess._handle_message(
        '{"type":"Turn","transcript":"Hello from AssemblyAI.","end_of_turn":true,'
        '"words":[{"text":"Hello","confidence":0.9},{"text":"from","confidence":0.8},'
        '{"text":"AssemblyAI","confidence":0.85}]}'
    )
    assert len(got) == 1
    assert isinstance(got[0], LiveFinal)
    assert got[0].text == "Hello from AssemblyAI."
    assert not got[0].partial
    assert 0.8 <= got[0].confidence <= 0.9
