"""Structured reviewer verdict — machine-readable review status.

The reviewer historically produced only a human markdown report, and the QA
loop decided pass/fail with a regex over that prose (fragile: misses an explicit
``## Status: FAIL``, false-positives on words like "previously failing", and
carries no actionable must-fix list). This module defines the small structured
verdict the reviewer now also writes (``{handoff_dir}/qa/review_result.json``)
and a tolerant reader for it, so the Definition-of-Done gate can judge quality
from structured truth instead of parsing English.

The reader is deliberately forgiving: a missing or malformed file returns
``None`` so callers degrade safely (fall back to the old signal) rather than
crash. See docs/CODING_STYLE.md Sections 9.6, 13.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from tools._path import handoff_dir_for
from utils.text import strip_code_fences

logger = logging.getLogger(__name__)

REVIEW_RESULT_FILENAME = "review_result.json"

# Canonical statuses. Anything we cannot confidently read as PASS is treated as
# FAIL by `passed` below — a review whose verdict is unclear must never count as
# a pass for the Definition-of-Done gate.
_PASS = "PASS"
_FAIL = "FAIL"
_PASS_WORDS = frozenset({"pass", "passed", "ok", "approved", "success"})
_FAIL_WORDS = frozenset({"fail", "failed", "reject", "rejected", "blocked"})


class ReviewTests(BaseModel):
    """The reviewer's test-run outcome, when it ran them."""

    passed: bool | None = None  # None = not run / unknown
    command: str | None = None


class ReviewVerification(BaseModel):
    """Bug-fix verification evidence, when the task was a bugfix/debug.

    All fields optional: absent (``None``) means "not recorded / unknown", which the
    Definition-of-Done gate treats as no evidence either way (it never fails a task
    just because a field is missing). The gate only acts on evidence that is present
    and *contradicts* a real fix (e.g. the reproduction did not pass after the fix).
    """

    reproduction_command: str | None = None  # the command/test that reproduces the bug
    pre_fix_failed: bool | None = None  # did it fail BEFORE the fix? (a real reproduction)
    post_fix_passed: bool | None = None  # does it pass AFTER the fix?
    regression_test_added: bool | None = None  # was a lasting test added/updated to guard it?
    regression_test_path: str | None = None


class ReviewResult(BaseModel):
    """A reviewer's machine-readable verdict for one review pass."""

    status: str = _FAIL
    must_fix: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    tests: ReviewTests = Field(default_factory=ReviewTests)
    verification: ReviewVerification = Field(default_factory=ReviewVerification)
    summary: str = ""

    @property
    def passed(self) -> bool:
        """True only when the review is an unambiguous PASS with nothing to fix.

        Conservative by design: an unclear status, any must-fix item, or a failed
        test run all mean "not passed" so the DoD gate never waves work through.
        """
        if self.status != _PASS:
            return False
        if self.must_fix:
            return False
        return self.tests.passed is not False

    @classmethod
    def parse(cls, data: dict[str, Any]) -> ReviewResult:
        """Build from a raw dict, coercing common shape variations."""
        return cls.model_validate(_coerce(data))


def _normalize_status(raw: Any, must_fix: list[str]) -> str:
    """Map a free-form status to PASS/FAIL, defaulting FAIL when unclear."""
    text = str(raw or "").strip().lower()
    if any(word in text for word in _FAIL_WORDS):
        return _FAIL
    if text in _PASS_WORDS or text == _PASS.lower():
        # An explicit pass with outstanding must-fix items is still a fail.
        return _PASS if not must_fix else _FAIL
    return _FAIL


def _as_str_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list | tuple):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _opt_bool(raw: Any) -> bool | None:
    """Return raw when it is a real bool, else None (unknown)."""
    return raw if isinstance(raw, bool) else None


def _opt_str(raw: Any) -> str | None:
    """Return a non-empty trimmed string, else None."""
    text = str(raw).strip() if raw is not None else ""
    return text or None


def _coerce(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"status": _FAIL}
    must_fix = _as_str_list(data.get("must_fix"))
    tests_raw = data.get("tests")
    tests = tests_raw if isinstance(tests_raw, dict) else {}
    ver_raw = data.get("verification")
    ver = ver_raw if isinstance(ver_raw, dict) else {}
    return {
        "status": _normalize_status(data.get("status"), must_fix),
        "must_fix": must_fix,
        "nice_to_have": _as_str_list(data.get("nice_to_have")),
        "tests": {
            "passed": _opt_bool(tests.get("passed")),
            "command": _opt_str(tests.get("command")),
        },
        "verification": {
            "reproduction_command": _opt_str(ver.get("reproduction_command")),
            "pre_fix_failed": _opt_bool(ver.get("pre_fix_failed")),
            "post_fix_passed": _opt_bool(ver.get("post_fix_passed")),
            "regression_test_added": _opt_bool(ver.get("regression_test_added")),
            "regression_test_path": _opt_str(ver.get("regression_test_path")),
        },
        "summary": str(data.get("summary") or ""),
    }


def review_result_path(task_id: str) -> Path:
    """Absolute path to a task's structured review result file."""
    return Path(handoff_dir_for(task_id)) / "qa" / REVIEW_RESULT_FILENAME


def read_review_result(task_id: str) -> ReviewResult | None:
    """Read a task's structured review verdict, or None if absent/unreadable.

    Tolerant on purpose: any problem (missing file, bad JSON, wrong shape)
    returns None so the caller falls back to its previous signal instead of
    failing the task on a formatting slip.
    """
    path = review_result_path(task_id)
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(strip_code_fences(text))
    except (ValueError, TypeError):
        logger.debug("review_result.json for task %s is not valid JSON", task_id)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ReviewResult.parse(data)
    except Exception:
        logger.debug("review_result.json for task %s has an unexpected shape", task_id, exc_info=True)
        return None
