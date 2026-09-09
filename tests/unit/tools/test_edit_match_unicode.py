"""Tests for _edit_match: line-ending/BOM helpers and the Unicode-tolerant pass.

Unicode matching is the last resort - a model emitting smart quotes or an em-dash
where the file has plain ASCII. Like every tolerant pass here, it is accepted
only when it lands in exactly one place; ambiguity must still fail loudly.
"""

from __future__ import annotations

from tools.specialized._edit_match import (
    detect_line_ending,
    find_unique,
    normalize_to_lf,
    normalize_unicode,
    restore_line_ending,
    split_bom,
)


def test_split_bom():
    assert split_bom("\ufeffhello") == ("\ufeff", "hello")
    assert split_bom("hello") == ("", "hello")


def test_detect_line_ending():
    assert detect_line_ending("a\r\nb") == "\r\n"
    assert detect_line_ending("a\nb") == "\n"
    assert detect_line_ending("no newline") == "\n"


def test_normalize_and_restore_round_trip():
    crlf = "a\r\nb\r\nc"
    lf = normalize_to_lf(crlf)
    assert lf == "a\nb\nc"
    assert restore_line_ending(lf, "\r\n") == crlf
    assert restore_line_ending(lf, "\n") == lf


def test_normalize_to_lf_handles_lone_cr():
    assert normalize_to_lf("a\rb\r\nc") == "a\nb\nc"


def test_normalize_unicode_folds_lookalikes():
    assert normalize_unicode("\u2018x\u2019") == "'x'"
    assert normalize_unicode("\u201cx\u201d") == '"x"'
    assert normalize_unicode("a\u2014b") == "a-b"  # em dash
    assert normalize_unicode("a\u00a0b") == "a b"  # non-breaking space


def test_exact_match_still_wins():
    content = "value = 1\n"
    match, error = find_unique(content, "value = 1")
    assert error == ""
    assert match is not None
    assert match.how == "exact"


def test_unicode_match_when_smart_quotes_differ():
    # File has ASCII quotes; the model's old_string has smart quotes.
    content = "name = 'north'\n"
    needle = "name = \u2018north\u2019"
    match, error = find_unique(content, needle)
    assert error == "", error
    assert match is not None
    assert match.how == "unicode"
    # Span covers the ASCII line in the original content.
    assert content[match.start : match.end] == "name = 'north'"


def test_unicode_match_em_dash():
    content = "title = 'a - b'\n"
    needle = "title = 'a \u2014 b'"  # em dash with spaces normalizes to "a - b"
    match, error = find_unique(content, needle)
    assert error == "", error
    assert match is not None
    assert match.how == "unicode"


def test_ambiguous_unicode_match_refuses():
    # Two ASCII lines both match once smart quotes are folded - must not guess.
    content = "x = 'a'\nx = 'a'\n"
    needle = "x = \u2018a\u2019"
    match, error = find_unique(content, needle)
    assert match is None
    assert "match" in error.lower()


def test_not_found_reports_closest_region():
    content = "alpha = 1\nbeta = 2\ngamma = 3\n"
    match, error = find_unique(content, "delta = 4")
    assert match is None
    assert "not found" in error.lower()
