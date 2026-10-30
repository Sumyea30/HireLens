"""Detect non-Latin script in transcript turns. Never logs turn text."""
from __future__ import annotations

LANGUAGE_MISMATCH_LABEL = "not English, check the transcript"
NON_LATIN_RATIO = 0.20


def _is_latin_letter(ch: str) -> bool:
    """Basic Latin + Latin-1 / Extended letters used in English orthography."""
    if not ch.isalpha():
        return False
    o = ord(ch)
    if o <= 0x024F:  # Latin, Latin-1 Supplement, Latin Extended-A/B
        return True
    if 0x1E00 <= o <= 0x1EFF:  # Latin Extended Additional
        return True
    return False


def non_latin_letter_ratio(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    non = sum(1 for c in letters if not _is_latin_letter(c))
    return non / len(letters)


def is_language_mismatch(text: str, *, threshold: float = NON_LATIN_RATIO) -> bool:
    """True when more than `threshold` of alphabetic characters are non-Latin script."""
    return non_latin_letter_ratio(text) > threshold


def apply_language_guard(turn: dict) -> dict:
    """Set language_mismatch on a turn dict. Does not log text."""
    text = str(turn.get("text") or "")
    mismatch = is_language_mismatch(text)
    turn["language_mismatch"] = mismatch
    return turn


def usable_for_prompts(turn: dict) -> bool:
    """Flagged turns stay out of Q&A / assessment prompts until an interviewer edits them."""
    if turn.get("language_mismatch") and not turn.get("edited"):
        return False
    return True
