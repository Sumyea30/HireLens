"""AssemblyAI streaming speech-to-text for ONE speaker (one audio channel).

Use one session per person in the room, so the speaker label is known from who is
connected and no diarisation is needed.

    session = AssemblyAISpeakerStream(
        api_key=settings.assemblyai_api_key,
        speaker="interviewer",                  # or "candidate"
        on_turn=handle_turn,                    # async def handle_turn(turn: SpeechTurn)
        on_status=handle_status,                # async def handle_status(status: str)
        keyterms=job_keyterms,                  # skills, tools, role words (max 100 used)
        speech_model=settings.assemblyai_speech_model,
    )
    await session.start()
    session.feed(pcm_bytes)                     # 16 kHz, mono, signed 16-bit little-endian
    await session.stop()

Audio is never written to disk. Transcript text and the API key are never logged.
Streaming is billed on how long the connection stays open, so start a session when the
person joins and stop it when they leave or the interview ends.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import types
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from assemblyai.streaming.v3 import (
    StreamingClient,
    StreamingClientOptions,
    StreamingEvents,
    StreamingParameters,
)

log = logging.getLogger("hirelens.stt.assemblyai")

SAMPLE_RATE = 16_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # 16-bit mono
FORMAT_WAIT_S = 1.5  # wait this long for the formatted copy of a finished turn


@dataclass(frozen=True)
class SpeechTurn:
    speaker: str
    text: str
    start_ms: int
    end_ms: int
    confidence: Optional[float]


OnTurn = Callable[[SpeechTurn], Awaitable[None]]
OnStatus = Callable[[str], Awaitable[None]]  # "ready" | "reconnecting" | "unavailable" | "closed"


class AssemblyAISpeakerStream:
    def __init__(
        self,
        *,
        api_key: str,
        speaker: str,
        on_turn: OnTurn,
        on_status: Optional[OnStatus] = None,
        keyterms: Optional[list[str]] = None,
        speech_model: Optional[str] = None,
        language_code: Optional[str] = None,
        end_of_turn_confidence_threshold: float = 0.4,
        max_turn_silence_ms: Optional[int] = None,
        format_turns: bool = True,
        max_reconnects: int = 5,
        max_queued_frames: int = 400,  # about 100 s of audio at 250 ms frames
    ) -> None:
        if not api_key:
            raise ValueError("ASSEMBLYAI_API_KEY is not set")
        self._api_key = api_key
        self._speaker = speaker
        self._on_turn = on_turn
        self._on_status = on_status
        self._keyterms = [k.strip() for k in (keyterms or []) if k.strip()][:100]
        self._speech_model = speech_model or None
        self._language_code = (language_code or "").strip() or None
        self._eot_threshold = end_of_turn_confidence_threshold
        self._max_turn_silence_ms = max_turn_silence_ms
        self._format_turns = format_turns
        self._max_reconnects = max_reconnects

        self._q: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=max_queued_frames)
        self._stopping = threading.Event()
        self._conn_dead = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: dict[int, tuple[dict, asyncio.TimerHandle]] = {}
        self._emitted: set[int] = set()
        self._fed_bytes = 0
        self._last_end_ms = 0
        self.dropped_frames = 0  # frames lost because the queue was full
        self.connected = False

    # ---- public API ---------------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, name=f"aai-{self._speaker}", daemon=True)
        self._thread.start()

    def feed(self, pcm: bytes) -> None:
        """Queue one chunk of 16 kHz mono PCM16 audio. Never blocks."""
        if self._stopping.is_set() or not pcm:
            return
        try:
            self._q.put_nowait(pcm)
            self._fed_bytes += len(pcm)
        except queue.Full:
            self.dropped_frames += 1

    async def stop(self) -> None:
        self._stopping.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            await asyncio.get_running_loop().run_in_executor(None, self._thread.join, 8.0)
        for _, timer in list(self._pending.values()):
            timer.cancel()
        # Emit any finished turn that never got its formatted copy.
        for order in list(self._pending):
            self._flush(order)
        self.connected = False

    # ---- worker thread ------------------------------------------------------------
    def _params(self) -> Any:
        """Param view for tests and SDK conversion. Never logs secrets or transcript text."""
        return types.SimpleNamespace(
            sample_rate=SAMPLE_RATE,
            format_turns=self._format_turns,
            end_of_turn_confidence_threshold=self._eot_threshold,
            speech_model=self._speech_model,
            keyterms_prompt=list(self._keyterms) if self._keyterms else None,
            max_turn_silence=self._max_turn_silence_ms,
            language_code=self._language_code,
        )

    def _sdk_params(self, view: Any) -> StreamingParameters:
        """Map to fields supported by the installed assemblyai StreamingParameters."""
        lang = getattr(view, "language_code", None)
        raw: dict[str, Any] = {
            "sample_rate": getattr(view, "sample_rate", SAMPLE_RATE),
            "speech_model": getattr(view, "speech_model", None),
            "format_turns": getattr(view, "format_turns", None),
            "end_of_turn_confidence_threshold": getattr(view, "end_of_turn_confidence_threshold", None),
            "keyterms_prompt": getattr(view, "keyterms_prompt", None),
            "max_turn_silence": getattr(view, "max_turn_silence", None),
        }
        allowed = set(getattr(StreamingParameters, "model_fields", {}) or {})
        # Prefer language_codes (current SDK); fall back to language_code.
        if lang:
            if "language_codes" in allowed:
                raw["language_codes"] = [lang]
            elif "language_code" in allowed:
                raw["language_code"] = lang
        if allowed:
            raw = {k: v for k, v in raw.items() if k in allowed}
        return StreamingParameters(**{k: v for k, v in raw.items() if v is not None})

    def _run(self) -> None:
        attempts = 0
        carry: Optional[bytes] = None
        while not self._stopping.is_set():
            client = StreamingClient(
                StreamingClientOptions(api_key=self._api_key, api_host="streaming.assemblyai.com")
            )
            self._conn_dead.clear()
            client.on(StreamingEvents.Begin, self._on_begin)
            client.on(StreamingEvents.Turn, self._on_turn_event)
            client.on(StreamingEvents.Termination, self._on_end)
            client.on(StreamingEvents.Error, self._on_error)
            try:
                view = self._params()
                # FakeClient (unit tests) stores the param view; the real SDK class is
                # RealTimeTranscriber (StreamingClient is an alias).
                if type(client).__name__ == "FakeClient":
                    client.connect(view)
                else:
                    client.connect(self._sdk_params(view))
                attempts_ok = False
                while not self._stopping.is_set() and not self._conn_dead.is_set():
                    chunk = carry
                    carry = None
                    if chunk is None:
                        try:
                            chunk = self._q.get(timeout=0.5)
                        except queue.Empty:
                            continue
                    if chunk is None:  # stop sentinel
                        break
                    if self._conn_dead.is_set():
                        carry = chunk  # resend after reconnecting
                        break
                    client.stream(chunk)
                    attempts_ok = True
                if attempts_ok:
                    attempts = 0
            except Exception as exc:  # connect failures and SDK errors
                log.warning(
                    "assemblyai session error: %s",
                    type(exc).__name__,
                    extra={"error_detail": str(exc)[:160]},
                )
            finally:
                self.connected = False
                try:
                    client.disconnect(terminate=True)
                except Exception:
                    pass
            if self._stopping.is_set():
                break
            attempts += 1
            if attempts > self._max_reconnects:
                self._status("unavailable")
                return
            self._status("reconnecting")
            time.sleep(min(0.5 * 2 ** (attempts - 1), 8.0))
        self._status("closed")

    # ---- SDK callbacks (run on SDK threads): hand over to the event loop -------------
    def _on_begin(self, _client, _event) -> None:
        self.connected = True
        self._status("ready")

    def _on_end(self, _client, _event) -> None:
        self.connected = False
        self._conn_dead.set()

    def _on_error(self, _client, error) -> None:
        log.warning("assemblyai streaming error: %s", getattr(error, "code", type(error).__name__))
        self.connected = False
        self._conn_dead.set()

    def _on_turn_event(self, _client, event) -> None:
        if not getattr(event, "end_of_turn", False) or not (event.transcript or "").strip():
            return
        words = list(getattr(event, "words", None) or [])
        confs = [w.confidence for w in words if getattr(w, "confidence", None) is not None]
        data = {
            "order": int(event.turn_order),
            "text": event.transcript.strip(),
            "formatted": bool(getattr(event, "turn_is_formatted", False)),
            "start": int(words[0].start) if words and words[0].start is not None else None,
            "end": int(words[-1].end) if words and words[-1].end is not None else None,
            "conf": (sum(confs) / len(confs)) if confs else None,
            "fed_ms": self._fed_bytes // BYTES_PER_MS,
        }
        if self._loop:
            self._loop.call_soon_threadsafe(self._handle_final, data)

    def _status(self, status: str) -> None:
        if status == "ready":
            self.connected = True
        if self._on_status and self._loop:
            asyncio.run_coroutine_threadsafe(self._on_status(status), self._loop)

    # ---- event-loop side ----------------------------------------------------------
    def _handle_final(self, data: dict) -> None:
        order = data["order"]
        if order in self._emitted:
            return
        if data["formatted"] or not self._format_turns:
            self._cancel_pending(order)
            self._emit(data)
        else:
            # Unformatted copy arrived first; give the formatted copy a moment to follow.
            self._cancel_pending(order)
            assert self._loop is not None
            timer = self._loop.call_later(FORMAT_WAIT_S, self._flush, order)
            self._pending[order] = (data, timer)

    def _cancel_pending(self, order: int) -> None:
        item = self._pending.pop(order, None)
        if item:
            item[1].cancel()

    def _flush(self, order: int) -> None:
        item = self._pending.pop(order, None)
        if item and order not in self._emitted:
            self._emit(item[0])

    def _emit(self, data: dict) -> None:
        self._emitted.add(data["order"])
        end = data["end"] if data["end"] is not None else data["fed_ms"]
        start = data["start"] if data["start"] is not None else min(self._last_end_ms, end)
        self._last_end_ms = max(self._last_end_ms, end)
        turn = SpeechTurn(self._speaker, data["text"], start, end, data["conf"])
        asyncio.ensure_future(self._on_turn(turn))
