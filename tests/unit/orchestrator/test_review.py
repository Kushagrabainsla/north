"""Tests for fix 2b: structured reviewer verdict (orchestrator/review.py)."""

from __future__ import annotations

import json

import pytest

from orchestrator import review as review_mod
from orchestrator.review import ReviewResult, read_review_result, review_result_path


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    """Point the handoff dir at a temp path so tests never touch ~/.north."""
    monkeypatch.setattr(review_mod, "handoff_dir_for", lambda task_id: str(tmp_path / task_id))
    return tmp_path


def _write(handoff_root, task_id: str, payload) -> None:
    path = review_result_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------- reader


def test_missing_file_returns_none(handoff):
    assert read_review_result("t1") is None


def test_malformed_json_returns_none(handoff):
    _write(handoff, "t1", "{not valid json")
    assert read_review_result("t1") is None


def test_valid_pass_is_read(handoff):
    _write(handoff, "t1", {
        "status": "PASS",
        "must_fix": [],
        "tests": {"passed": True, "command": "pytest -q"},
        "summary": "all good",
    })
    r = read_review_result("t1")
    assert r is not None
    assert r.status == "PASS"
    assert r.passed is True
    assert r.tests.command == "pytest -q"


def test_fail_with_must_fix(handoff):
    _write(handoff, "t1", {"status": "FAIL", "must_fix": ["foo.py:10 - null not handled"]})
    r = read_review_result("t1")
    assert r.status == "FAIL"
    assert r.passed is False
    assert r.must_fix == ["foo.py:10 - null not handled"]


# ---------------------------------------------------------------- verdict logic


def test_json_fenced_content_is_tolerated(handoff):
    _write(handoff, "t1", "```json\n{\"status\": \"PASS\", \"must_fix\": []}\n```")
    r = read_review_result("t1")
    assert r is not None and r.passed is True


@pytest.mark.parametrize("raw,expected", [("pass", "PASS"), ("PASS", "PASS"), ("passed", "PASS"),
                                          ("fail", "FAIL"), ("FAILED", "FAIL"), ("weird", "FAIL")])
def test_status_normalization(raw, expected):
    assert ReviewResult.parse({"status": raw}).status == expected


def test_pass_with_must_fix_is_coerced_to_fail():
    # An explicit "PASS" that still lists must-fix items is not a real pass.
    r = ReviewResult.parse({"status": "PASS", "must_fix": ["x:1 - bug"]})
    assert r.status == "FAIL"
    assert r.passed is False


def test_pass_but_tests_failed_is_not_passed():
    r = ReviewResult.parse({"status": "PASS", "must_fix": [], "tests": {"passed": False}})
    assert r.passed is False


def test_verification_block_is_parsed_when_present():
    r = ReviewResult.parse(
        {
            "status": "PASS",
            "verification": {
                "reproduction_command": "pytest tests/test_bug.py::test_x",
                "pre_fix_failed": True,
                "post_fix_passed": True,
                "regression_test_added": True,
                "regression_test_path": "tests/test_bug.py",
            },
        }
    )
    assert r.verification.reproduction_command == "pytest tests/test_bug.py::test_x"
    assert r.verification.pre_fix_failed is True
    assert r.verification.post_fix_passed is True
    assert r.verification.regression_test_added is True
    assert r.verification.regression_test_path == "tests/test_bug.py"


def test_verification_defaults_to_unknown_when_absent():
    r = ReviewResult.parse({"status": "PASS"})
    assert r.verification.post_fix_passed is None
    assert r.verification.regression_test_added is None
    assert r.verification.reproduction_command is None


def test_verification_ignores_non_bool_and_empty_values():
    r = ReviewResult.parse(
        {"status": "PASS", "verification": {"post_fix_passed": "yes", "regression_test_path": "  "}}
    )
    # A non-bool must become None (unknown), not a truthy coercion; blank path -> None.
    assert r.verification.post_fix_passed is None
    assert r.verification.regression_test_path is None


def test_must_fix_string_is_coerced_to_list():
    r = ReviewResult.parse({"status": "FAIL", "must_fix": "one problem"})
    assert r.must_fix == ["one problem"]


def test_defaults_are_conservative():
    # An empty/near-empty verdict must never read as a pass.
    r = ReviewResult.parse({})
    assert r.status == "FAIL"
    assert r.passed is False


def test_write_then_read_roundtrip(handoff):
    _write(handoff, "task-xyz", {"status": "PASS", "must_fix": [], "nice_to_have": ["tidy names"]})
    r = read_review_result("task-xyz")
    assert r.passed is True
    assert r.nice_to_have == ["tidy names"]
