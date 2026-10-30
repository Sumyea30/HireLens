"""WebRTC signalling for a two-person interview.

Media stays in the browsers. This socket forwards offers, answers and ICE
candidates, and tells each side when the other person joins or leaves.
A second connection from the same person replaces the first, so a refresh
does not leave a ghost peer in the room.

PCM16 audio frames are transcribed server-side. Transcript, notes, co-pilot
and score messages go only to staff connections. Raw audio is never stored.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket

from app.api.access import can_see_job
from app.core.auth import verify_token
from app.services.audio_pipeline import AudioHub, InterviewAudio
from app.services.stt import AssemblyAiStt, FakeStt, HttpStt
from app.services.transcript import record_speech

log = logging.getLogger("hirelens.room")
router = APIRouter()

MAX_PEERS = 2
REPLACED = 4000
STAFF = frozenset({"interviewer", "admin"})


class RoomHub:
    def __init__(self):
        self._rooms: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    async def join(self, interview_id: str, ws: WebSocket, user_id: str, role: str) -> bool:
        """Register this socket. False when two other people are already in the room."""
        async with self._lock:
            peers = self._rooms.setdefault(interview_id, [])
            stale = [p for p in peers if p["user_id"] == user_id]
            peers[:] = [p for p in peers if p["user_id"] != user_id]
            if len(peers) >= MAX_PEERS:
                full = True
                others: list[dict] = []
            else:
                peers.append({"ws": ws, "user_id": user_id, "role": role})
                others = [p for p in peers if p["ws"] is not ws]
                full = False
        for old in stale:
            await _close(old["ws"], REPLACED)
        if full:
            await _close(ws, 4403)
            return False
        if others:
            await _send(ws, {"type": "peer", "state": "joined"})
            for other in others:
                await _send(other["ws"], {"type": "peer", "state": "joined"})
        return True

    async def leave(self, interview_id: str, ws: WebSocket) -> None:
        async with self._lock:
            peers = self._rooms.get(interview_id, [])
            if not any(p["ws"] is ws for p in peers):
                return
            peers = [p for p in peers if p["ws"] is not ws]
            if peers:
                self._rooms[interview_id] = peers
            else:
                self._rooms.pop(interview_id, None)
        for other in peers:
            await _send(other["ws"], {"type": "peer", "state": "left"})

    async def relay(self, interview_id: str, ws: WebSocket, message: dict) -> None:
        async with self._lock:
            peers = [p for p in self._rooms.get(interview_id, []) if p["ws"] is not ws]
        for other in peers:
            await _send(other["ws"], message)

    async def broadcast(self, interview_id: str, message: dict) -> None:
        async with self._lock:
            peers = list(self._rooms.get(interview_id, []))
        for other in peers:
            await _send(other["ws"], message)

    async def broadcast_staff(self, interview_id: str, message: dict) -> None:
        """Transcript, status and tool data: staff only."""
        async with self._lock:
            peers = [p for p in self._rooms.get(interview_id, []) if p.get("role") in STAFF]
        for other in peers:
            await _send(other["ws"], message)

    def staff_present(self, interview_id: str) -> bool:
        peers = self._rooms.get(interview_id, [])
        return any(p.get("role") in STAFF for p in peers)


async def _send(ws: WebSocket, message: dict) -> None:
    try:
        await ws.send_json(message)
    except Exception:
        return


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code=code)
    except Exception:
        return


def hub_for(ws: WebSocket) -> RoomHub:
    app = ws.scope["app"]
    hub = getattr(app.state, "rooms", None)
    if hub is None:
        hub = RoomHub()
        app.state.rooms = hub
    return hub


def audio_hub_for(ws: WebSocket) -> AudioHub:
    app = ws.scope["app"]
    hub = getattr(app.state, "audio", None)
    if hub is None:
        hub = AudioHub()
        app.state.audio = hub
    return hub


def _stt_for(services):
    existing = getattr(services, "stt", None)
    if existing is not None:
        return existing
    settings = services.settings
    provider = (getattr(settings, "stt_provider", "") or "faster_whisper").strip().lower()
    timeout_s = float(getattr(settings, "stt_timeout_seconds", 60))
    if provider in ("fake", "browser"):
        # browser: captions come from SpeechRecognition → room "speech" messages (staff-only).
        stt = FakeStt()
        if provider == "browser":
            log.info("stt_provider_browser", extra={"note": "final phrases via websocket speech events"})
    elif provider in ("assemblyai", "assembly_ai", "aai"):
        stt = AssemblyAiStt(
            getattr(settings, "assemblyai_api_key", "") or "",
            base_url=getattr(settings, "assemblyai_base_url", "") or "https://api.assemblyai.com",
            timeout_s=timeout_s,
            language_code=getattr(settings, "stt_language", "") or "en",
        )
    else:
        stt = HttpStt(getattr(settings, "stt_url", "") or "", timeout_s)
    services.stt = stt
    return stt


@router.websocket("/ws/interview/{interview_id}")
async def interview_room(ws: WebSocket, interview_id: str):
    await ws.accept()
    hub = hub_for(ws)
    audio_hub = audio_hub_for(ws)
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        message = json.loads(raw)
    except Exception:
        await _close(ws, 4401)
        return
    if message.get("type") != "auth":
        await _close(ws, 4401)
        return
    services = ws.scope["app"].state.services
    settings = services.settings
    store = services.store
    user = verify_token(settings.auth_secret, str(message.get("token") or ""))
    row = store.get_interview(interview_id)
    if not user or not row:
        await _close(ws, 4401)
        return
    role = user["role"]
    if role == "candidate":
        allowed = user["sub"] == row["candidate_id"]
    else:
        allowed = can_see_job(user, store.get_job(row["job_id"]))
    if not allowed or row["status"] == "completed":
        await _close(ws, 4403)
        return
    admitted = False
    try:
        admitted = await hub.join(interview_id, ws, user["sub"], role)
        if not admitted:
            return

        # Staff hard-refresh clears React state; push any saved turns so the panel is not empty.
        if role in STAFF:
            doc = store.transcript_document(interview_id) or {}
            for turn in doc.get("turns") or []:
                await _send(ws, {"type": "transcript", "turn": turn})

        async def on_turn(turn: dict) -> None:
            # Staff only — candidates must never see transcript text.
            room_audio = audio_hub.get(interview_id)
            pending = room_audio.pending() if room_audio else 0
            await hub.broadcast_staff(interview_id, {"type": "transcript", "turn": turn})
            # Keep interviewer status in sync even if an earlier listening ping was missed.
            await hub.broadcast_staff(interview_id, {
                "type": "stt_status",
                "state": "listening" if pending == 0 else "transcribing",
                "pending": pending,
                "message": "",
            })

        async def on_status(payload: dict) -> None:
            await hub.broadcast_staff(interview_id, payload)

        async def on_activity(payload: dict) -> None:
            await hub.broadcast_staff(interview_id, payload)

        def make_audio() -> InterviewAudio:
            job = store.get_job(row["job_id"])
            return InterviewAudio(
                interview_id, store, _stt_for(services),
                job=job, on_turn=on_turn, on_status=on_status, on_activity=on_activity,
            )

        while True:
            incoming = await ws.receive()
            if incoming["type"] == "websocket.disconnect":
                break
            raw_bytes = incoming.get("bytes")
            if raw_bytes is not None:
                # Transcribe; discard after the utterance is processed. Never write to disk.
                speaker = "candidate" if role == "candidate" else "interviewer"
                room_audio = audio_hub.ensure(interview_id, make_audio)
                room_audio.push(speaker, raw_bytes)
                continue
            text = incoming.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            kind = payload.get("type")
            if kind == "ready":
                await hub.relay(interview_id, ws, {"type": "peer", "state": "ready"})
            elif kind == "signal" and payload.get("kind") in ("offer", "answer", "ice"):
                await hub.relay(interview_id, ws, {"type": "signal", "kind": payload["kind"], "data": payload.get("data")})
            elif kind == "speech":
                # Kept for tests and mock mode. Production browsers send PCM frames instead.
                spoken = " ".join(str(payload.get("text") or "").split())
                if spoken:
                    turn = record_speech(store, interview_id, role, spoken[:4000])
                    if turn:
                        await hub.broadcast_staff(interview_id, {"type": "transcript", "turn": turn})
    except Exception:
        log.info("room_closed", extra={"interview_id": interview_id})
    finally:
        if admitted:
            await hub.leave(interview_id, ws)
            async with hub._lock:
                empty = interview_id not in hub._rooms
            if empty:
                await audio_hub.discard(interview_id)
