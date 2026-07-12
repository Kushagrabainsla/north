"""Definition-of-Done evaluator for engineering tasks.

A deterministic, evidence-backed check that answers one question: is this coding
result actually done to a trustworthy bar? It reads only recorded evidence -
which model produced each result (see AgentResult.models_used) and the reviewer's
structured verdict (orchestrator/review.py) - never prose or the model's word.

This module is pure: it takes already-gathered evidence and returns a verdict.
The orchestrator gathers the evidence and decides what to do with the verdict
(warn-only first, then enforcing). See docs/CODING_STYLE.md Sections 4.1, 13.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from orchestrator.review import ReviewResult

# Kinds for which a fix should be backed by reproduction + a regression test. Kept
# here (not imported from the router) so this module stays pure and dependency-free.
_BUGFIX_KINDS: frozenset[str] = frozenset({"bugfix", "debug"})


class DodResult(BaseModel):
    """Verdict of a Definition-of-Done evaluation."""

    passed: bool
    reasons: list[str] = Field(default_factory=list)  # why it did NOT pass; empty when passed


def _bugfix_evidence_reasons(review: ReviewResult) -> list[str]:
    """Reasons a bugfix/debug result fails on its *recorded* verification evidence.

    Conservative and fail-open: evidence that is absent (``None``) never produces a
    reason - the reviewer may simply not have filled it in. Only evidence that is
    present and *contradicts* a real, verified fix counts against the task. (The
    orchestrator-run executable oracle in a later step provides the stronger,
    presence-required check.)
    """
    reasons: list[str] = []
    v = review.verification
    if v.post_fix_passed is False:
        reasons.append("the fix is unverified - the recorded reproduction did not pass after the fix")
    if v.reproduction_command and v.pre_fix_failed is False:
        reasons.append(
            "the bug was not reproduced before fixing - the reproduction passed pre-fix, "
            "so the change is unverified"
        )
    if v.regression_test_added is False:
        reasons.append("no regression test was added to guard the fix against reappearing")
    return reasons


def evaluate_engineering_dod(
    *,
    change_applied: bool,
    coder_models: list[str],
    reviewer_models: list[str],
    review: ReviewResult | None,
    kind: str | None = None,
    auto_verify_passed: bool | None = None,
) -> DodResult:
    """Return whether an engineering task meets the Definition of Done.

    Passes only when ALL hold:
      1. a code change was actually applied,
      2. a structured review verdict exists and passed (tests ok, no must-fix),
      3. the review was an *independent* second opinion - a different model than
         the coder used (this is what makes the rubber-duck real),
      4. for a bugfix/debug, no *recorded* verification evidence contradicts a real
         fix (a reproduction that still fails, a "bug" that never failed pre-fix, or
         an explicitly-absent regression test),
      5. the orchestrator's own independent verification run did not fail (an
         executable oracle, not the model's word).

    Conservative: anything unproven counts against passing on checks 1-3, so a task
    can never be declared done on missing or ambiguous evidence. Checks 4 and 5 are
    deliberately fail-open on *absent* evidence (``auto_verify_passed is None`` and
    absent bugfix fields never fail); only a recorded contradiction or a real
    non-zero verification exit counts against the task.
    """
    reasons: list[str] = []

    if not change_applied:
        reasons.append("no code change was applied")

    if review is None:
        reasons.append("no structured review verdict was produced")
    elif not review.passed:
        if review.must_fix:
            reasons.append(f"review did not pass - {len(review.must_fix)} must-fix item(s) unresolved")
        elif review.tests.passed is False:
            reasons.append("review did not pass - tests failed")
        else:
            reasons.append("review did not pass")

    # Independence check only applies when a review actually happened.
    if review is not None:
        if not reviewer_models:
            reasons.append("reviewer model was not recorded - cannot confirm an independent review")
        elif not coder_models:
            reasons.append("coder model was not recorded - cannot confirm an independent review")
        elif set(coder_models) & set(reviewer_models):
            shared = ", ".join(sorted(set(coder_models) & set(reviewer_models)))
            reasons.append(
                f"review used the same model as the coder ({shared}) - not an independent second opinion"
            )

    # Kind-specific fix evidence (bugfix/debug), only on recorded contradictions.
    if review is not None and (kind or "").strip().lower() in _BUGFIX_KINDS:
        reasons.extend(_bugfix_evidence_reasons(review))

    # Independent executable oracle: only a real failure (False) counts; unknown
    # (None, e.g. no command detected or the harness errored) never fails the task.
    if auto_verify_passed is False:
        reasons.append("independent verification failed - the orchestrator's own test run did not pass")

    return DodResult(passed=not reasons, reasons=reasons)
