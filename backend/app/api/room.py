"""WebRTC signalling for a two-person interview.

Media stays in the browsers. This socket forwards offers, answers and ICE
candidates, and tells each side when the other person joins or leaves.
A second connection from the same person replaces the first, so a refresh
does not leave a ghost peer in the room. Spoken text from the browser is
stored and broadcast as transcript turns; raw audio bytes are dropped.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket

from app.api.access import can_see_job
from app.core.auth import verify_token
from app.services.transcript import record_speech

log = logging.getLogger("hirelens.room")
router = APIRouter()

MAX_PEERS = 2
REPLACED = 4000


class RoomHub:
    def __init__(self):
        self._rooms: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    async def join(self, interview_id: str, ws: WebSocket, user_id: str) -> bool:
        """Register this socket. False when two other people are already in the room."""
        async with self._lock:
            peers = self._rooms.setdefault(interview_id, [])
            stale = [p for p in peers if p["user_id"] == user_id]
            peers[:] = [p for p in peers if p["user_id"] != user_id]
            if len(peers) >= MAX_PEERS:
                full = True
                others: list[dict] = []
            else:
                peers.append({"ws": ws, "user_id": user_id})
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


@router.websocket("/ws/interview/{interview_id}")
async def interview_room(ws: WebSocket, interview_id: str):
    await ws.accept()
    hub = hub_for(ws)
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        message = json.loads(raw)
    except Exception:
        await _close(ws, 4401)
        return
    if message.get("type") != "auth":
        await _close(ws, 4401)
        return
    settings = ws.scope["app"].state.services.settings
    store = ws.scope["app"].state.services.store
    user = verify_token(settings.auth_secret, str(message.get("token") or ""))
    row = store.get_interview(interview_id)
    if not user or not row:
        await _close(ws, 4401)
        return
    if user["role"] == "candidate":
        allowed = user["sub"] == row["candidate_id"]
    else:
        allowed = can_see_job(user, store.get_job(row["job_id"]))
    if not allowed or row["status"] == "completed":
        await _close(ws, 4403)
        return
    admitted = False
    try:
        admitted = await hub.join(interview_id, ws, user["sub"])
        if not admitted:
            return
        while True:
            incoming = await ws.receive()
            if incoming["type"] == "websocket.disconnect":
                break
            if incoming.get("bytes") is not None:
                # Audio is not stored. Drop the chunk as soon as it arrives.
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
                text = " ".join(str(payload.get("text") or "").split())
                if text:
                    turn = record_speech(store, interview_id, user["role"], text[:4000])
                    if turn:
                        await hub.broadcast(interview_id, {"type": "transcript", "turn": turn})
    except Exception:
        log.info("room_closed", extra={"interview_id": interview_id})
    finally:
        if admitted:
            await hub.leave(interview_id, ws)
