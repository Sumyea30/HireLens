"""Language mismatch guard: Devanagari and Bengali samples. Never asserts on logging text."""
from app.services.language_guard import (
    LANGUAGE_MISMATCH_LABEL,
    apply_language_guard,
    is_language_mismatch,
    usable_for_prompts,
)


def test_english_is_not_mismatch():
    assert not is_language_mismatch("I have five years of experience with Python and React.")


def test_devanagari_sample_is_mismatch():
    # Hindi (Devanagari): "मेरा नाम राज है"
    text = "मेरा नाम राज है और मैं सॉफ्टवेयर इंजीनियर हूँ"
    assert is_language_mismatch(text)
    turn = apply_language_guard({"text": text, "speaker": "candidate"})
    assert turn["language_mismatch"] is True
    assert not usable_for_prompts(turn)
    assert LANGUAGE_MISMATCH_LABEL.startswith("not English")


def test_bengali_sample_is_mismatch():
    # Bengali: "আমার নাম রহিম"
    text = "আমার নাম রহিম এবং আমি সফটওয়্যার ইঞ্জিনিয়ার"
    assert is_language_mismatch(text)
    turn = apply_language_guard({"text": text})
    assert turn["language_mismatch"] is True


def test_mixed_mostly_latin_is_ok():
    # A few non-Latin chars under 20% should not flag.
    text = "Hello world cafe " + "म" * 2
    assert not is_language_mismatch(text)


def test_edited_flagged_turn_becomes_usable():
    turn = {"text": "मेरा नाम राज है", "language_mismatch": True, "edited": True}
    assert usable_for_prompts(turn)


def test_empty_and_digits_only_are_not_mismatch():
    assert not is_language_mismatch("")
    assert not is_language_mismatch("12345 !!!")
