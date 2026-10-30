"""Optional first-10s PCM dump for diagnosing garbage captions. Off by default.

Enable with STT_DEBUG_DUMP=1. Writes 16 kHz mono WAV under a git-ignored folder,
logs a warning when enabled, and deletes files older than 10 minutes.
Never log transcript text or API keys.
"""
from __future__ import annotations

import logging
import os
import struct
import time
from pathlib import Path

log = logging.getLogger("hirelens.audio")

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
DUMP_SECONDS = 10
DUMP_BYTES = SAMPLE_RATE * BYTES_PER_SAMPLE * DUMP_SECONDS
TTL_S = 10 * 60


def debug_dump_enabled() -> bool:
    return os.environ.get("STT_DEBUG_DUMP", "0").strip().lower() in ("1", "true", "yes")


def dump_dir() -> Path:
    raw = os.environ.get("STT_DEBUG_DUMP_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "stt_debug_dumps"


def _pcm16_mono_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    n = len(pcm)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + n,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        n,
    ) + pcm


class SpeakerDump:
    """Accumulate the first DUMP_SECONDS of PCM for one speaker, then seal a WAV."""

    def __init__(self, interview_id: str, speaker: str):
        self.interview_id = interview_id
        self.speaker = speaker
        self._buf = bytearray()
        self._done = False
        self.path: Path | None = None

    def feed(self, pcm: bytes) -> None:
        if self._done or not pcm:
            return
        need = DUMP_BYTES - len(self._buf)
        if need <= 0:
            self._done = True
            return
        self._buf.extend(pcm[:need])
        if len(self._buf) >= DUMP_BYTES:
            self._seal()

    def _seal(self) -> None:
        self._done = True
        folder = dump_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.interview_id)[:40]
            path = folder / f"{safe_id}_{self.speaker}_{int(time.time())}.wav"
            path.write_bytes(_pcm16_mono_to_wav(bytes(self._buf)))
            self.path = path
            log.warning(
                "stt_debug_dump_wrote",
                extra={"interview_id": self.interview_id, "speaker": self.speaker, "bytes": len(self._buf)},
            )
        except Exception:
            log.warning("stt_debug_dump_failed", extra={"error_type": "write"})
        finally:
            self._buf.clear()
        purge_old_dumps()


_warned = False


def warn_if_enabled() -> None:
    global _warned
    if not debug_dump_enabled() or _warned:
        return
    _warned = True
    log.warning(
        "stt_debug_dump_enabled",
        extra={"dir": str(dump_dir()), "ttl_minutes": TTL_S // 60},
    )


def purge_old_dumps() -> None:
    folder = dump_dir()
    if not folder.is_dir():
        return
    cutoff = time.time() - TTL_S
    try:
        for path in folder.glob("*.wav"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass
