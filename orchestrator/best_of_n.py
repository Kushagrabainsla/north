"""Best-of-N candidate selection for the coder (#11).

When ``best_of_n > 1``, the orchestrator runs several independent coder attempts,
each in its own isolated git worktree, then integrates only the best one. This
module holds the *deterministic* selection logic, kept pure so it is trivially
testable: given the outcome of each candidate, pick the winner.

Ranking (best first):
1. viable candidates - the run succeeded AND produced changes;
2. among those, ones whose tests passed, then untested, then failed;
3. then the smallest change (a surgical diff beats a sprawling one);
4. then the lowest index (stable, so selection is reproducible).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateOutcome:
    index: int
    succeeded: bool  # the agent run completed without raising
    changed: bool  # it produced a non-empty diff
    tests_passed: bool | None  # None when no test command was run
    diff_lines: int  # magnitude of the change (insertions + deletions)


def _test_rank(passed: bool | None) -> int:
    if passed is True:
        return 2
    if passed is None:
        return 1
    return 0  # explicitly failed


def _score(c: CandidateOutcome) -> tuple:
    viable = c.succeeded and c.changed
    # Higher tuple sorts better. Negate diff_lines/index so smaller wins.
    return (
        1 if viable else 0,
        _test_rank(c.tests_passed),
        -c.diff_lines,
        -c.index,
    )


def select_best(candidates: list[CandidateOutcome]) -> int | None:
    """Return the index of the best candidate, or None for an empty list.

    Always returns an index when at least one candidate exists; viability and
    quality are encoded in the ranking, so the caller can treat the winner as
    "the one to integrate" and handle a non-viable winner as a failed run.
    """
    if not candidates:
        return None
    best = max(candidates, key=_score)
    return best.index


def any_viable(candidates: list[CandidateOutcome]) -> bool:
    """True when at least one candidate succeeded and produced changes."""
    return any(c.succeeded and c.changed for c in candidates)
