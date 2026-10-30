"""Speech-to-text providers. Raw audio stays in memory only (never written locally)."""
from __future__ import annotations

import logging
import os
import re
import struct
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

import httpx

log = logging.getLogger("hirelens.stt")

ASSEMBLYAI_DEFAULT_BASE = "https://api.assemblyai.com"

# Known Whisper silence hallucinations (short, high-confidence nonsense).
_HALLUCINATIONS = re.compile(
    r"^\s*(thank you\.?|thanks for watching\.?|thanks for listening\.?|"
    r"subscribe\.?|you\.?|bye\.?|okay\.?|ok\.?|hmm\.?|uh+|um+|"
    r"we just think it'?s time to say it.*|"
    r"subtitles by .*|the end\.?|"
    r"clear interview english\.?|"
    r"technical hiring conversation\.?)\s*$",
    re.IGNORECASE,
)
# Auto-detect / multilingual models invent non-Latin scripts on noise or accents.
_NON_LATIN = re.compile(
    r"[\u0900-\u097f\u0980-\u09ff\u0a00-\u0a7f\u0a80-\u0aff"
    r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7a3]"
)
# "응, 응, 응…" / "say it, say it, say it…" / "to you and to you and…"
_REPEATED_TOKEN = re.compile(r"^(?P<t>.{1,48}?)(?:\s*[,.]?\s*(?P=t)){3,}\s*$", re.IGNORECASE | re.UNICODE)
_REPEATED_AND = re.compile(r"^(?P<t>.{2,40}?)(?:\s+and\s+(?P=t)){3,}", re.IGNORECASE | re.UNICODE)
_LOW_CONFIDENCE = 0.35  # UI tint only


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Looser defaults so Bangladeshi-accented English is kept; override via env.
def high_no_speech() -> float:
    return _env_float("STT_HIGH_NO_SPEECH", 0.78)


def low_avg_logprob() -> float:
    return _env_float("STT_LOW_AVG_LOGPROB", -1.25)


def min_keep_confidence() -> float:
    return _env_float("STT_MIN_KEEP_CONFIDENCE", 0.05)


@dataclass
class SttSegment:
    text: str
    start_s: float
    end_s: float
    confidence: float
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0


@dataclass
class SttResult:
    segments: list[SttSegment] = field(default_factory=list)
    model: str = ""
    real_time_factor: float | None = None
    fast_mode: bool = False
    error: str | None = None


class SttProvider(Protocol):
    def transcribe(self, pcm16_mono_16k: bytes, *, prompt: str = "") -> SttResult: ...


class FakeStt:
    """Deterministic provider for tests. UTF-8 text after a 4-byte magic header, else a fixed phrase."""

    MAGIC = b"TXT\0"

    def __init__(self, default: str = "I built services with Python."):
        self.default = default
        self.calls = 0
        self.fail = False

    def transcribe(self, pcm16_mono_16k: bytes, *, prompt: str = "") -> SttResult:
        self.calls += 1
        if self.fail:
            return SttResult(error="unavailable")
        if pcm16_mono_16k.startswith(self.MAGIC):
            text = pcm16_mono_16k[len(self.MAGIC):].decode("utf-8", errors="ignore").strip()
        else:
            text = self.default
        duration = max(0.4, len(pcm16_mono_16k) / (16000 * 2))
        return SttResult(segments=[SttSegment(text=text, start_s=0.0, end_s=duration, confidence=0.9)],
                         model="fake", real_time_factor=0.1)


class HttpStt:
    def __init__(self, base_url: str, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def transcribe(self, pcm16_mono_16k: bytes, *, prompt: str = "") -> SttResult:
        if not self.base_url:
            return SttResult(error="not_configured")
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                res = client.post(
                    f"{self.base_url}/transcribe",
                    files={"audio": ("audio.pcm", pcm16_mono_16k, "application/octet-stream")},
                    data={"prompt": prompt, "sample_rate": "16000"},
                )
            if res.status_code >= 500:
                log.warning("stt_failed", extra={"error_type": f"HTTP{res.status_code}"})
                return SttResult(error=f"HTTP{res.status_code}")
            if res.status_code >= 400:
                return SttResult(error=f"HTTP{res.status_code}")
            data = res.json()
        except Exception as exc:
            log.warning("stt_failed", extra={"error_type": type(exc).__name__})
            return SttResult(error=type(exc).__name__)
        segments = []
        for s in data.get("segments") or []:
            segments.append(SttSegment(
                text=str(s.get("text") or "").strip(),
                start_s=float(s.get("start") or 0),
                end_s=float(s.get("end") or 0),
                confidence=_confidence(s),
                no_speech_prob=float(s.get("no_speech_prob") or 0),
                avg_logprob=float(s.get("avg_logprob") or 0),
            ))
        return SttResult(
            segments=segments,
            model=str(data.get("model") or ""),
            real_time_factor=data.get("real_time_factor"),
            fast_mode=bool(data.get("fast_mode")),
        )


def pcm16_mono_to_wav(pcm16_mono_16k: bytes, sample_rate: int = 16000) -> bytes:
    """Minimal WAV wrapper so AssemblyAI can decode in-memory PCM (no local file)."""
    n = len(pcm16_mono_16k)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + n,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        1,  # mono
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        n,
    ) + pcm16_mono_16k


class AssemblyAiStt:
    """AssemblyAI STT. Live rooms use Universal Streaming; batch upload is a fallback."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = ASSEMBLYAI_DEFAULT_BASE,
        timeout_s: float = 90.0,
        language_code: str = "en",
        poll_interval_s: float = 0.35,
        speech_model: str | None = None,
        live: bool | None = None,
    ):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or ASSEMBLYAI_DEFAULT_BASE).rstrip("/")
        self.timeout_s = timeout_s
        self.language_code = (language_code or "en").strip() or "en"
        self.poll_interval_s = max(0.15, poll_interval_s)
        self.speech_model = (speech_model or os.environ.get(
            "ASSEMBLYAI_SPEECH_MODEL", "universal-streaming-english"
        )).strip()
        # Batch upload is the reliable path; streaming can open yet emit zero turns.
        if live is None:
            live = os.environ.get("ASSEMBLYAI_LIVE", "0").strip().lower() in ("1", "true", "yes")
        self.live_capable = bool(live)

    def open_live(self, on_final, *, label: str = ""):
        """Legacy websocket session (tests). Prefer open_speaker_stream for rooms."""
        from app.services.assemblyai_live import AssemblyAiLiveSession
        return AssemblyAiLiveSession(
            self.api_key,
            on_final,
            speech_model=self.speech_model,
            language_code=self.language_code,
            label=label,
        )

    def open_speaker_stream(self, *, speaker: str, on_turn, on_status=None, keyterms: list[str] | None = None):
        """One AssemblyAI streaming session per speaker (SDK v3)."""
        from app.services.assemblyai_stream import AssemblyAISpeakerStream
        return AssemblyAISpeakerStream(
            api_key=self.api_key,
            speaker=speaker,
            on_turn=on_turn,
            on_status=on_status,
            keyterms=keyterms,
            speech_model=self.speech_model,
            language_code=self.language_code or "en",
        )

    def transcribe(self, pcm16_mono_16k: bytes, *, prompt: str = "") -> SttResult:
        del prompt  # AssemblyAI word_boost is optional; live captions skip it to avoid bias.
        if not self.api_key:
            return SttResult(error="not_configured")
        if not pcm16_mono_16k:
            return SttResult(error="empty_audio")
        duration_s = max(0.05, len(pcm16_mono_16k) / (16000 * 2))
        headers = {"authorization": self.api_key}
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                upload = client.post(
                    f"{self.base_url}/v2/upload",
                    headers=headers,
                    content=pcm16_mono_to_wav(pcm16_mono_16k),
                )
                if upload.status_code >= 400:
                    log.warning("stt_failed", extra={"error_type": f"upload_HTTP{upload.status_code}"})
                    return SttResult(error=f"upload_HTTP{upload.status_code}")
                upload_url = (upload.json() or {}).get("upload_url")
                if not upload_url:
                    return SttResult(error="upload_missing_url")

                payload: dict = {
                    "audio_url": upload_url,
                    # Force English — never auto-detect Hindi/etc.
                    "language_code": self.language_code or "en",
                    "language_detection": False,
                }
                created = client.post(
                    f"{self.base_url}/v2/transcript",
                    headers={**headers, "content-type": "application/json"},
                    json=payload,
                )
                if created.status_code >= 400:
                    log.warning("stt_failed", extra={"error_type": f"create_HTTP{created.status_code}"})
                    return SttResult(error=f"create_HTTP{created.status_code}")
                tid = (created.json() or {}).get("id")
                if not tid:
                    return SttResult(error="create_missing_id")

                deadline = started + self.timeout_s
                data: dict = {}
                while time.monotonic() < deadline:
                    polled = client.get(f"{self.base_url}/v2/transcript/{tid}", headers=headers)
                    if polled.status_code >= 400:
                        return SttResult(error=f"poll_HTTP{polled.status_code}")
                    data = polled.json() or {}
                    status = str(data.get("status") or "")
                    if status == "completed":
                        break
                    if status == "error":
                        err = str(data.get("error") or "transcript_error")
                        log.warning("stt_failed", extra={"error_type": "assemblyai_error"})
                        return SttResult(error=err[:120])
                    time.sleep(self.poll_interval_s)
                else:
                    return SttResult(error="timeout")
        except Exception as exc:
            log.warning("stt_failed", extra={"error_type": type(exc).__name__})
            return SttResult(error=type(exc).__name__)

        elapsed = max(0.001, time.monotonic() - started)
        text = str(data.get("text") or "").strip()
        conf = data.get("confidence")
        try:
            confidence = max(0.0, min(1.0, float(conf))) if conf is not None else 0.85
        except (TypeError, ValueError):
            confidence = 0.85
        # Prefer the completed transcript text. Word arrays can be empty/partial
        # even when `text` is set; never drop a good caption because of that.
        words = data.get("words") or []
        word_segments = _segments_from_assembly_words(words) if words else []
        if text:
            segments = [SttSegment(text=text, start_s=0.0, end_s=duration_s, confidence=confidence)]
            if word_segments and word_segments[0].text:
                # Keep word timings when they match the same utterance.
                segments = word_segments
        else:
            segments = word_segments
        return SttResult(
            segments=segments,
            model="assemblyai-universal",
            real_time_factor=elapsed / duration_s,
            fast_mode=False,
        )


def _segments_from_assembly_words(words: list) -> list[SttSegment]:
    """Collapse word timings into one caption segment (utterances are already short)."""
    parts: list[str] = []
    confs: list[float] = []
    start_ms = None
    end_ms = 0.0
    for w in words:
        t = str(w.get("text") or "").strip()
        if not t:
            continue
        parts.append(t)
        try:
            confs.append(float(w.get("confidence")))
        except (TypeError, ValueError):
            pass
        try:
            s = float(w.get("start") or 0)
            e = float(w.get("end") or s)
        except (TypeError, ValueError):
            continue
        if start_ms is None:
            start_ms = s
        end_ms = e
    text = " ".join(parts).strip()
    if not text:
        return []
    confidence = sum(confs) / len(confs) if confs else 0.85
    return [SttSegment(
        text=text,
        start_s=(start_ms or 0.0) / 1000.0,
        end_s=end_ms / 1000.0,
        confidence=max(0.0, min(1.0, confidence)),
    )]


def _confidence(seg: dict) -> float:
    if "confidence" in seg and seg["confidence"] is not None:
        return max(0.0, min(1.0, float(seg["confidence"])))
    # Map avg_logprob (~0 good, ~-1 poor) into 0..1
    alp = float(seg.get("avg_logprob") or -0.5)
    return max(0.0, min(1.0, 1.0 + alp))


def _is_repetitive(text: str) -> bool:
    t = text.strip()
    if _REPEATED_TOKEN.match(t) or _REPEATED_AND.match(t):
        return True
    words = re.findall(r"[A-Za-z0-9']+", t.lower())
    if len(words) < 8:
        return False
    for n in (2, 3, 4):
        if len(words) < n * 4:
            continue
        grams = [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
        top, count = Counter(grams).most_common(1)[0]
        if count >= 4 and count * n >= len(words) * 0.45:
            return True
    return False


def _looks_like_skills_list(text: str) -> bool:
    """Whisper often echoes comma-separated job requirements as if spoken."""
    if text.count(",") < 3:
        return False
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 4:
        return False
    return all(len(p) < 80 for p in parts) and len(text) < 400


def drop_reason(seg: SttSegment) -> str | None:
    """Why a segment is discarded. None means keep. Never includes the spoken text."""
    if not seg.text or len(seg.text) < 2:
        return "too_short"
    if seg.no_speech_prob >= high_no_speech():
        return "no_speech"
    if seg.avg_logprob and seg.avg_logprob < low_avg_logprob():
        return "low_logprob"
    if seg.confidence < min_keep_confidence():
        return "low_confidence"
    if _HALLUCINATIONS.match(seg.text):
        return "hallucination"
    if _is_repetitive(seg.text):
        return "repeated_token"
    if _looks_like_skills_list(seg.text):
        return "prompt_echo"
    # Non-Latin script is kept and flagged language_mismatch for interviewer edit —
    # do not drop here (see language_guard).
    return None


def keep_segment(seg: SttSegment) -> bool:
    return drop_reason(seg) is None


def vocabulary_prompt(job: dict | None) -> str:
    """Live STT must not send job skills as Whisper initial_prompt — the model echoes them as speech.

    Kept as an empty stub so callers and tests stay stable. Technical terms are better handled
    by editing turns after the fact than by poisoning the decoder prompt.
    """
    return ""


def is_low_confidence(confidence: float | None) -> bool:
    return confidence is not None and confidence < _LOW_CONFIDENCE
