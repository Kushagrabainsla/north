from __future__ import annotations

from memory.dedup import normalize_for_dedup


def test_normalization_ignores_case_punctuation_and_filler() -> None:
    assert normalize_for_dedup("The user's Dog is named Rex!") == "user s dog named rex"
    assert normalize_for_dedup("A dog  named   Rex.") == "dog named rex"


def test_two_restatements_normalize_identically() -> None:
    assert normalize_for_dedup("I have a meeting on Monday") == normalize_for_dedup("I have the meeting Monday!")


def test_filler_only_input_normalizes_to_empty() -> None:
    assert normalize_for_dedup("the a an of and") == ""
