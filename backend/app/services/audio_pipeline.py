"""Per-speaker PCM buffers, silence cuts, and a short transcription queue.

Raw audio is held only until the utterance is transcribed, then discarded.
AssemblyAI live rooms stream PCM continuously (no local silence-cut batching).
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.services.stt import SttProvider, SttResult, drop_reason, vocabulary_prompt
from app.services.transcript import edit_turn_text, record_utterance

log = logging.getLogger("hirelens.audio")

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
MAX_QUEUE = 60


def _stt_workers() -> int:
    try:
        return max(1, min(4, int(os.environ.get("STT_WORKERS", "2"))))
    except (TypeError, ValueError):
        return 2


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def silence_rms() -> float:
    return _env_float("STT_SILENCE_RMS", 200.0)


def silence_ms() -> float:
    return _env_float("STT_SILENCE_MS", 500.0)


def max_utterance_s() -> float:
    return _env_float("STT_MAX_UTTERANCE_S", 8.0)


def min_speech_s() -> float:
    return _env_float("STT_MIN_SPEECH_S", 0.25)


@dataclass
class QueuedUtterance:
    speaker: str
    pcm: bytes
    start_sample: int
    end_sample: int


OnTurn = Callable[[dict], Awaitable[None]]
OnStatus = Callable[[dict], Awaitable[None]]
OnActivity = Callable[[dict], Awaitable[None]]


@dataclass
class SpeakerBuf:
    samples: bytearray = field(default_factory=bytearray)
    start_sample: int = 0
    total_samples: int = 0
    speech_seen: bool = False
    quiet_ms: float = 0.0
    speech_samples: int = 0
    loud_ms: float = 0.0  # consecutive above-threshold frames before arming


class InterviewAudio:
    """One interview: two speaker buffers and a single-file transcription queue."""

    def __init__(
        self,
        interview_id: str,
        store,
        stt: SttProvider,
        *,
        job: dict | None = None,
        on_turn: OnTurn | None = None,
        on_status: OnStatus | None = None,
        on_activity: OnActivity | None = None,
    ):
        self.interview_id = interview_id
        self.store = store
        self.stt = stt
        self.job = job
        self.on_turn = on_turn
        self.on_status = on_status
        self.on_activity = on_activity
        self._speakers: dict[str, SpeakerBuf] = {
            "interviewer": SpeakerBuf(),
            "candidate": SpeakerBuf(),
        }
        self._queue: asyncio.Queue[QueuedUtterance | None] = asyncio.Queue(maxsize=MAX_QUEUE)
        self._dropped = 0
        self._worker: asyncio.Task | None = None
        self._workers: list[asyncio.Task] = []
        self._closed = False
        self._unavailable_notified = False
        self._fast_notified = False
        self._listening_sent = False
        # Drop immediate Whisper loops of the same phrase from one speaker.
        # Live: (text, ms, turn_id). Batch: (text, ms) or (text, ms, "").
        self._last_kept: dict[str, tuple] = {}
        self._live: dict = {}
        self._live_results: asyncio.Queue | None = None
        self._live_drain: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sample_clock = 0
        self._debug_dumps: dict = {}

    def _maybe_debug_dump(self, role: str, pcm: bytes) -> None:
        try:
            from app.services.stt_debug_dump import SpeakerDump, debug_dump_enabled, warn_if_enabled
        except Exception:
            return
        if not debug_dump_enabled():
            return
        warn_if_enabled()
        dump = self._debug_dumps.get(role)
        if dump is None:
            dump = SpeakerDump(self.interview_id, role)
            self._debug_dumps[role] = dump
        dump.feed(pcm)

    def start(self) -> None:
        if self._worker is not None or self._live:
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        if getattr(self.stt, "live_capable", False) and hasattr(self.stt, "open_live"):
            self._start_live()
        else:
            n = _stt_workers()
            self._workers = [asyncio.create_task(self._run()) for _ in range(n)]
            self._worker = self._workers[0]
            self._emit_status("listening", pending=0)

    def _start_live(self) -> None:
        # Sessions are created on first audio for each role (avoids idle WS + rate limits).
        self._live = {"_enabled": True}
        self._live_results = asyncio.Queue()
        self._live_drain = asyncio.create_task(self._drain_live())
        self._emit_status("listening", pending=0)

    def _job_keyterms(self) -> list[str]:
        terms: list[str] = []
        for req in (self.job or {}).get("requirements") or []:
            raw = str(req.get("text") or req.get("skill") or "").strip()
            if raw:
                terms.append(raw[:48])
        title = str((self.job or {}).get("title") or "").strip()
        if title:
            terms.insert(0, title[:48])
        return terms[:100]

    def _ensure_live_session(self, role: str):
        if not self._live or role in self._live:
            return self._live.get(role) if self._live else None
        # Prefer SDK speaker stream; fall back to legacy websocket session.
        if hasattr(self.stt, "open_speaker_stream") and self._loop:
            async def on_turn(turn) -> None:
                self._enqueue_live_result(
                    role,
                    turn.text,
                    float(turn.confidence or 0.85),
                    False,
                )

            async def on_status(status: str) -> None:
                if status == "unavailable":
                    self._unavailable_notified = True
                    self._emit_status(
                        "unavailable",
                        pending=0,
                        message="Live transcription could not connect. The call continues.",
                    )
                elif status == "ready":
                    self._unavailable_notified = False
                    self._emit_status("listening", pending=0, message="")

            stream = self.stt.open_speaker_stream(
                speaker=role,
                on_turn=on_turn,
                on_status=on_status,
                keyterms=self._job_keyterms(),
            )
            adapter = _SpeakerStreamAdapter(stream)
            self._live[role] = adapter
            self._loop.create_task(self._boot_speaker_stream(role, stream))
            return adapter

        session = self.stt.open_live(
            lambda final, r=role: self._on_live_final(r, final),
            label=role,
        )
        session.start()
        self._live[role] = session
        if self._loop and not self._unavailable_notified:
            self._loop.call_later(8.0, lambda: self._check_live_connected(role))
        return session

    async def _boot_speaker_stream(self, role: str, stream) -> None:
        try:
            await stream.start()
        except Exception:
            log.warning("stt_failed", extra={"error_type": "speaker_stream_start"})
            if not self._unavailable_notified:
                self._unavailable_notified = True
                self._emit_status(
                    "unavailable",
                    pending=0,
                    message="Live transcription could not connect. The call continues.",
                )
            return
        if self._loop and not self._unavailable_notified:
            self._loop.call_later(8.0, lambda: self._check_live_connected(role))

    def _check_live_connected(self, role: str) -> None:
        if self._closed or self._unavailable_notified:
            return
        session = (self._live or {}).get(role)
        if session is None or getattr(session, "connected", False):
            return
        self._unavailable_notified = True
        self._emit_status(
            "unavailable",
            pending=0,
            message="Live transcription could not connect. The call continues.",
        )

    def _on_live_final(self, role: str, final) -> None:
        text = str(getattr(final, "text", "") or "").strip()
        if not text:
            return
        conf = float(getattr(final, "confidence", 0.85) or 0.85)
        partial = bool(getattr(final, "partial", False))
        q = self._live_results
        loop = self._loop
        if q is None or loop is None or not loop.is_running():
            log.warning("stt_failed", extra={"error_type": "live_loop_missing"})
            return
        try:
            loop.call_soon_threadsafe(self._enqueue_live_result, role, text, conf, partial)
        except Exception:
            log.warning("stt_failed", extra={"error_type": "live_enqueue_failed"})

    def _enqueue_live_result(self, role: str, text: str, conf: float, partial: bool = False) -> None:
        q = self._live_results
        if q is None:
            return
        try:
            q.put_nowait((role, text, conf, partial))
        except Exception:
            log.warning("stt_failed", extra={"error_type": "live_queue_full"})

    async def _drain_live(self) -> None:
        assert self._live_results is not None
        while True:
            item = await self._live_results.get()
            if item is None:
                return
            role, text, conf, partial = item if len(item) == 4 else (*item, False)
            seg_conf = max(0.0, min(1.0, conf))
            now_ms = int(self._sample_clock * 1000 / SAMPLE_RATE)
            # Ephemeral partial caption — do not persist until end_of_turn.
            if partial:
                if self.on_turn:
                    await self.on_turn({
                        "turn_id": f"partial-{role}",
                        "speaker": role,
                        "text": text[:4000],
                        "start_ms": max(0, now_ms - 1500),
                        "end_ms": now_ms,
                        "confidence": seg_conf,
                    })
                continue
            from app.services.stt import SttSegment
            reason = drop_reason(SttSegment(text=text, start_s=0, end_s=1, confidence=seg_conf))
            if reason:
                log.info(
                    "stt_segment_dropped",
                    extra={
                        "interview_id": self.interview_id,
                        "speaker": role,
                        "reason": reason,
                        "chars": len(text),
                    },
                )
                continue
            prev = self._last_kept.get(role)
            # prev: (text, start_ms, turn_id)
            if prev and prev[0] == text and now_ms - prev[1] < 8000:
                continue
            # Glue short same-speaker fragments into one caption line.
            prev_id = prev[2] if prev and len(prev) > 2 else ""
            if (
                prev
                and prev_id
                and not str(prev_id).startswith("partial-")
                and now_ms - prev[1] < 3500
                and len(prev[0]) < 60
                and len(text) < 60
            ):
                merged = f"{prev[0].rstrip()} {text}".strip()[:4000]
                try:
                    turn = edit_turn_text(self.store, self.interview_id, prev_id, merged)
                except Exception:
                    turn = None
                if turn:
                    self._last_kept[role] = (merged, now_ms, prev_id)
                    if self.on_turn:
                        await self.on_turn(turn)
                    continue
            try:
                turn = record_utterance(
                    self.store, self.interview_id, role, text[:4000],
                    start_ms=max(0, now_ms - 2000), end_ms=now_ms, confidence=seg_conf,
                )
            except Exception:
                log.warning("stt_failed", extra={"error_type": "record_utterance"})
                continue
            if turn and self.on_turn:
                self._last_kept[role] = (text, now_ms, str(turn.get("turn_id") or ""))
                self._emit_status("listening", pending=0)
                try:
                    await self.on_turn(turn)
                    log.info(
                        "stt_turn_broadcast",
                        extra={
                            "interview_id": self.interview_id,
                            "speaker": role,
                            "chars": len(text),
                            "turn_id": turn.get("turn_id"),
                        },
                    )
                except Exception:
                    log.warning("stt_failed", extra={"error_type": "on_turn_broadcast"})

    async def close(self) -> None:
        self._closed = True
        if self._live:
            for key, session in list(self._live.items()):
                if key == "_enabled" or session is True:
                    continue
                try:
                    session.close()
                except Exception:
                    pass
            self._live.clear()
            if self._live_results is not None:
                try:
                    self._live_results.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            if self._live_drain:
                try:
                    await asyncio.wait_for(self._live_drain, timeout=3)
                except Exception:
                    self._live_drain.cancel()
                self._live_drain = None
            for buf in self._speakers.values():
                buf.samples.clear()
            return
        for role in list(self._speakers):
            self._flush(role, force=True)
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        tasks = list(self._workers) or ([self._worker] if self._worker else [])
        for _ in tasks[1:]:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        for task in tasks:
            if not task:
                continue
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                task.cancel()
        self._workers = []
        self._worker = None
        for buf in self._speakers.values():
            buf.samples.clear()

    def pending(self) -> int:
        if self._live:
            return 0
        return self._queue.qsize()

    def push(self, speaker: str, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        role = "candidate" if speaker == "candidate" else "interviewer"
        n = len(pcm) // BYTES_PER_SAMPLE
        self._sample_clock += n
        self._maybe_debug_dump(role, pcm)
        rms = _rms(pcm)
        if self._live:
            session = self._ensure_live_session(role)
            if session is not None:
                session.push(pcm)
            # Activity pill for staff UI.
            buf = self._speakers[role]
            if rms >= silence_rms():
                was = buf.speech_seen
                buf.speech_seen = True
                if not was:
                    self._emit_activity(True, role)
            elif buf.speech_seen:
                buf.speech_seen = False
                self._emit_activity(False, role)
            return
        buf = self._speakers[role]
        if not buf.samples:
            buf.start_sample = buf.total_samples
        buf.samples.extend(pcm)
        buf.total_samples += n
        frame_ms = n * 1000.0 / SAMPLE_RATE
        if rms >= silence_rms():
            buf.loud_ms += frame_ms
            buf.quiet_ms = 0.0
            # Require ~200 ms of sustained energy so keyboard/fan clicks
            # do not flush empty AssemblyAI clips.
            if not buf.speech_seen and buf.loud_ms >= 200.0:
                buf.speech_seen = True
                self._emit_activity(True, role)
            if buf.speech_seen:
                buf.speech_samples += n
        else:
            buf.loud_ms = 0.0
            if buf.speech_seen:
                buf.quiet_ms += frame_ms
        duration_s = len(buf.samples) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        if buf.speech_seen and (buf.quiet_ms >= silence_ms() or duration_s >= max_utterance_s()):
            self._flush(role, force=False)

    def _flush(self, role: str, *, force: bool) -> None:
        buf = self._speakers[role]
        if not buf.samples:
            return
        samples = len(buf.samples) // BYTES_PER_SAMPLE
        min_samples = int(min_speech_s() * SAMPLE_RATE)
        speech_ratio = (buf.speech_samples / samples) if samples else 0.0
        if not force and (not buf.speech_seen or samples < min_samples):
            if force:
                buf.samples.clear()
                buf.speech_seen = False
                buf.quiet_ms = 0.0
                buf.speech_samples = 0
                buf.loud_ms = 0.0
            return
        if buf.speech_seen:
            self._emit_activity(False, role)
        # Trim trailing silence (~half the silence budget) when cutting on pause.
        trim_s = min(0.25, silence_ms() / 2000.0) if not force else 0.0
        trim = int(trim_s * SAMPLE_RATE) * BYTES_PER_SAMPLE
        end = max(0, len(buf.samples) - trim)
        pcm = bytes(buf.samples[:end] if end else buf.samples)
        start_sample = buf.start_sample
        end_sample = start_sample + len(pcm) // BYTES_PER_SAMPLE
        speech_samples = buf.speech_samples
        buf.samples.clear()
        buf.speech_seen = False
        buf.quiet_ms = 0.0
        buf.speech_samples = 0
        buf.loud_ms = 0.0
        buf.start_sample = buf.total_samples
        if len(pcm) < min_samples * BYTES_PER_SAMPLE:
            return
        # Skip near-silence / room-noise buffers — they yield empty AssemblyAI results.
        pcm_rms = _rms(pcm)
        spoken = speech_samples / max(1, len(pcm) // BYTES_PER_SAMPLE)
        if pcm_rms < silence_rms() * 1.15 or spoken < 0.18:
            log.info(
                "stt_skip_quiet rms=%.0f speech_ratio=%.2f audio_s=%.2f",
                pcm_rms,
                spoken,
                len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE),
            )
            return
        item = QueuedUtterance(role, pcm, start_sample, end_sample)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                pass
            if self.on_status and self._dropped in (1, 5, 10):
                self._emit_status(
                    "transcribing",
                    pending=self.pending(),
                    message="Transcription is behind; oldest audio was dropped. Accuracy may miss some turns.",
                )
        self._emit_status("transcribing", pending=self.pending())

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            await self._transcribe(item)
            left = self.pending()
            self._emit_status("listening" if left == 0 else "transcribing", pending=left)

    async def _transcribe(self, item: QueuedUtterance) -> None:
        audio_s = len(item.pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
        prompt = vocabulary_prompt(self.job)
        pcm_rms = _rms(item.pcm)
        pcm_peak = _peak(item.pcm)
        # Keep one debug clip so empty AssemblyAI results can be inspected locally.
        try:
            from app.services.stt import pcm16_mono_to_wav
            debug_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs")
            os.makedirs(debug_dir, exist_ok=True)
            with open(os.path.join(debug_dir, "last_utterance.wav"), "wb") as fh:
                fh.write(pcm16_mono_to_wav(item.pcm))
        except Exception:
            pass
        t0 = time.perf_counter()
        try:
            result: SttResult = await asyncio.to_thread(
                self.stt.transcribe, item.pcm, prompt=prompt)
        except Exception as exc:
            log.warning("stt_failed", extra={"error_type": type(exc).__name__})
            result = SttResult(error=type(exc).__name__)
        finally:
            item.pcm = b""  # discard audio immediately
        stt_s = time.perf_counter() - t0

        if result.error:
            log.info(
                "stt_utterance",
                extra={
                    "interview_id": self.interview_id,
                    "speaker": item.speaker,
                    "audio_s": round(audio_s, 3),
                    "stt_s": round(stt_s, 3),
                    "chars": 0,
                    "kept_chars": 0,
                    "error": result.error,
                },
            )
            if self.on_status and not self._unavailable_notified:
                self._unavailable_notified = True
                self._emit_status(
                    "unavailable",
                    pending=self.pending(),
                    message="Transcription unavailable. The call continues.",
                )
            return
        if result.fast_mode and self.on_status and not self._fast_notified:
            self._fast_notified = True
            self._emit_status(
                "transcribing",
                pending=self.pending(),
                message="Transcription is running slowly on CPU; captions may lag.",
            )

        chars = sum(len(s.text or "") for s in result.segments)
        kept_chars = 0
        base_ms = int(item.start_sample * 1000 / SAMPLE_RATE)
        for seg in result.segments:
            reason = drop_reason(seg)
            if reason:
                log.info(
                    "stt_segment_dropped",
                    extra={
                        "interview_id": self.interview_id,
                        "speaker": item.speaker,
                        "reason": reason,
                        "chars": len(seg.text or ""),
                        "confidence": round(seg.confidence, 3),
                        "no_speech_prob": round(seg.no_speech_prob, 3),
                        "avg_logprob": round(seg.avg_logprob, 3) if seg.avg_logprob else None,
                    },
                )
                continue
            start_ms = base_ms + int(seg.start_s * 1000)
            end_ms = base_ms + int(max(seg.end_s, seg.start_s) * 1000)
            text = seg.text[:4000]
            prev = self._last_kept.get(item.speaker)
            if prev and prev[0] == text and start_ms - prev[1] < 8000:
                log.info(
                    "stt_segment_dropped",
                    extra={
                        "interview_id": self.interview_id,
                        "speaker": item.speaker,
                        "reason": "duplicate",
                        "chars": len(text),
                    },
                )
                continue
            turn = record_utterance(
                self.store, self.interview_id, item.speaker,
                text, start_ms=start_ms, end_ms=end_ms, confidence=seg.confidence,
            )
            if turn and self.on_turn:
                self._last_kept[item.speaker] = (text, start_ms, str(turn.get("turn_id") or ""))
                kept_chars += len(text)
                try:
                    await self.on_turn(turn)
                    log.info("stt_turn_sent id=%s chars=%s", turn.get("turn_id"), len(text))
                except Exception:
                    log.warning("stt_failed", extra={"error_type": "on_turn_broadcast"})

        log.info(
            "stt_utterance audio=%.2fs stt=%.2fs rms=%.0f peak=%s chars=%s kept=%s model=%s",
            audio_s,
            stt_s,
            pcm_rms,
            pcm_peak,
            chars,
            kept_chars,
            result.model or "?",
        )

    def _emit_status(self, state: str, *, pending: int, message: str = "") -> None:
        if not self.on_status:
            return
        if state == "listening":
            self._listening_sent = True
        payload = {"type": "stt_status", "state": state, "pending": pending, "message": message}
        try:
            asyncio.get_running_loop().create_task(self.on_status(payload))
        except RuntimeError:
            pass

    def _emit_activity(self, active: bool, speaker: str) -> None:
        if not self.on_activity:
            return
        payload = {"type": "speech_activity", "active": active, "speaker": speaker}
        try:
            asyncio.get_running_loop().create_task(self.on_activity(payload))
        except RuntimeError:
            pass


def _rms(pcm: bytes) -> float:
    if len(pcm) < 4:
        return 0.0
    n = len(pcm) // 2
    total = 0
    for i in range(0, n * 2, 2):
        sample = struct.unpack_from("<h", pcm, i)[0]
        total += sample * sample
    return (total / n) ** 0.5


def _peak(pcm: bytes) -> int:
    if len(pcm) < 2:
        return 0
    peak = 0
    for i in range(0, len(pcm) - 1, 2):
        sample = abs(struct.unpack_from("<h", pcm, i)[0])
        if sample > peak:
            peak = sample
    return peak


class _SpeakerStreamAdapter:
    """Sync push/close face for AssemblyAISpeakerStream inside InterviewAudio."""

    def __init__(self, stream):
        self._stream = stream

    @property
    def connected(self) -> bool:
        return bool(getattr(self._stream, "connected", False))

    def push(self, pcm: bytes) -> None:
        self._stream.feed(pcm)

    def start(self) -> None:
        return None

    def close(self) -> None:
        stream = self._stream
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(stream.stop())


class AudioHub:
    """Interview_id -> InterviewAudio. Created lazily on first audio frame."""

    def __init__(self):
        self._rooms: dict[str, InterviewAudio] = {}
        self._lock = asyncio.Lock()

    def get(self, interview_id: str) -> InterviewAudio | None:
        return self._rooms.get(interview_id)

    def ensure(self, interview_id: str, factory) -> InterviewAudio:
        room = self._rooms.get(interview_id)
        # Hard refresh can race discard/close; never keep pushing into a closed room.
        if room is None or getattr(room, "_closed", False):
            room = factory()
            room.start()
            self._rooms[interview_id] = room
        return room

    async def discard(self, interview_id: str) -> None:
        async with self._lock:
            room = self._rooms.pop(interview_id, None)
        if room:
            await room.close()
