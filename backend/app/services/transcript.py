"""Seal the spoken transcript when an interview ends.

The JSON document is the record. A file is written only when TRANSCRIPT_DIR is set.
That folder is git-ignored. Raw audio is never written.
"""
import json
import pathlib
from datetime import datetime, timezone


def seal_transcript(store, settings, interview_id: str) -> dict:
    row = store.get_interview(interview_id) or {}
    doc = store.transcript_document(interview_id) or {
        "interview_id": interview_id,
        "candidate_id": row.get("candidate_id") or "",
        "job_id": row.get("job_id") or "",
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "turns": [],
    }
    doc["interview_id"] = interview_id
    doc["candidate_id"] = row.get("candidate_id") or doc.get("candidate_id") or ""
    doc["job_id"] = row.get("job_id") or doc.get("job_id") or ""
    doc["started_at"] = row.get("started_at") or doc.get("started_at")
    doc["ended_at"] = row.get("ended_at") or doc.get("ended_at")
    doc["turns"] = list(doc.get("turns") or [])
    path = _write(getattr(settings, "transcript_dir", ""), doc)
    store.save_transcript(interview_id, doc, path or row.get("transcript_file"))
    return doc


def record_speech(store, interview_id: str, speaker: str, text: str) -> dict | None:
    """Legacy helper for tests: wall-clock timestamps. Prefer record_utterance."""
    started = store.ensure_started(interview_id)
    end_ms = _elapsed_ms(started)
    start_ms = max(0, end_ms - min(8000, max(400, len(text) * 50)))
    return record_utterance(store, interview_id, speaker, text, start_ms=start_ms, end_ms=end_ms)


def record_utterance(
    store,
    interview_id: str,
    speaker: str,
    text: str,
    *,
    start_ms: int,
    end_ms: int,
    confidence: float | None = None,
) -> dict | None:
    """Append one spoken turn with stream-based timestamps."""
    from app.services.language_guard import apply_language_guard

    store.ensure_started(interview_id)
    text = " ".join((text or "").split())
    if not text:
        return None
    turn = {
        "speaker": "candidate" if speaker == "candidate" else "interviewer",
        "start_ms": max(0, int(start_ms)),
        "end_ms": max(int(start_ms), int(end_ms)),
        "text": text,
    }
    if confidence is not None:
        turn["confidence"] = max(0.0, min(1.0, float(confidence)))
    apply_language_guard(turn)
    return store.append_turn(interview_id, turn)


def edit_turn_text(store, interview_id: str, turn_id: str, text: str) -> dict | None:
    """Interviewer correction before seal. Keeps the first original in original_text.

    Re-runs the language guard after the edit so a fixed Latin turn is usable in prompts.
    """
    text = " ".join((text or "").split())
    if not text:
        return None
    return store.edit_turn(interview_id, turn_id, text)


def _elapsed_ms(started_at: str | None) -> int:
    if not started_at:
        return 0
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))


def _write(folder: str, doc: dict) -> str | None:
    folder = (folder or "").strip()
    interview_id = str(doc.get("interview_id") or "")
    if not folder or not interview_id.startswith("int-") or any(c in interview_id for c in "/\\."):
        return None
    path = pathlib.Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    dest = path / f"{interview_id}.json"
    dest.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(dest)
