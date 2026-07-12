"""Tests for shared text utilities - em/en dash normalization and prose gating."""

from __future__ import annotations

from utils.text import normalize_dashes, should_normalize_prose


def test_em_dash_becomes_spaced_hyphen_regardless_of_original_spacing():
    assert normalize_dashes("north—the OS") == "north - the OS"
    assert normalize_dashes("north — the OS") == "north - the OS"
    assert normalize_dashes("north —the OS") == "north - the OS"


def test_en_dash_becomes_hyphen_preserving_spacing():
    assert normalize_dashes("pages 10–20") == "pages 10-20"
    assert normalize_dashes("a – b") == "a - b"


def test_horizontal_bar_is_also_normalized():
    assert normalize_dashes("a―b") == "a - b"


def test_plain_hyphen_and_markdown_structure_are_untouched():
    # Hyphen-minus, frontmatter/rule ---, and bullet dashes must survive intact.
    assert normalize_dashes("well-tested") == "well-tested"
    assert normalize_dashes("---\nname: x\n---") == "---\nname: x\n---"
    assert normalize_dashes("- item one\n- item two") == "- item one\n- item two"


def test_newlines_are_never_crossed():
    # A dash at line start must not pull the previous line up (no \n eaten).
    assert normalize_dashes("line one\n—line two") == "line one\n - line two"
    assert normalize_dashes("a\n\nb") == "a\n\nb"


def test_empty_and_dashless_text_pass_through():
    assert normalize_dashes("") == ""
    assert normalize_dashes("no dashes here") == "no dashes here"


def test_should_normalize_prose_gates_on_extension():
    # Prose/doc and unknown types are normalized; code/data files are protected.
    assert should_normalize_prose("report.md")
    assert should_normalize_prose("notes.txt")
    assert should_normalize_prose("briefing")  # no extension
    assert not should_normalize_prose("module.py")
    assert not should_normalize_prose("data.json")
    assert not should_normalize_prose("rows.csv")
    assert not should_normalize_prose("config.yaml")
    assert not should_normalize_prose("Module.PY")  # case-insensitive
