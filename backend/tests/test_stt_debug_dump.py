"""STT debug dump: first 10 s WAV, TTL purge. No network."""
import time
from pathlib import Path

from app.services.stt_debug_dump import DUMP_BYTES, SpeakerDump, dump_dir, purge_old_dumps


def test_speaker_dump_writes_wav_after_ten_seconds(tmp_path, monkeypatch):
    monkeypatch.setenv("STT_DEBUG_DUMP", "1")
    monkeypatch.setenv("STT_DEBUG_DUMP_DIR", str(tmp_path))
    dump = SpeakerDump("int-test", "interviewer")
    chunk = b"\x00\x01" * 800  # 800 samples
    while not dump._done:
        dump.feed(chunk)
    assert dump.path is not None
    assert dump.path.exists()
    assert dump.path.stat().st_size >= 44 + DUMP_BYTES
    raw = dump.path.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"


def test_purge_removes_old_wavs(tmp_path, monkeypatch):
    monkeypatch.setenv("STT_DEBUG_DUMP_DIR", str(tmp_path))
    old = tmp_path / "old.wav"
    old.write_bytes(b"RIFF" + b"\x00" * 40)
    # Make mtime old
    past = time.time() - 11 * 60
    import os
    os.utime(old, (past, past))
    fresh = tmp_path / "fresh.wav"
    fresh.write_bytes(b"RIFF" + b"\x00" * 40)
    purge_old_dumps()
    assert not old.exists()
    assert fresh.exists()
