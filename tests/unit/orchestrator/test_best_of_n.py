"""Tests for best-of-N candidate selection (#11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.best_of_n import CandidateOutcome, any_viable, select_best
from orchestrator.worktree import Worktree


def _c(index, succeeded=True, changed=True, tests_passed=None, diff_lines=10):
    return CandidateOutcome(
        index=index,
        succeeded=succeeded,
        changed=changed,
        tests_passed=tests_passed,
        diff_lines=diff_lines,
    )


def test_empty_returns_none():
    assert select_best([]) is None


def test_prefers_viable_over_nonviable():
    cands = [_c(0, succeeded=False), _c(1, changed=False), _c(2)]
    assert select_best(cands) == 2


def test_passing_tests_beats_untested_and_failed():
    cands = [_c(0, tests_passed=False), _c(1, tests_passed=None), _c(2, tests_passed=True)]
    assert select_best(cands) == 2


def test_untested_beats_failed():
    cands = [_c(0, tests_passed=False, diff_lines=1), _c(1, tests_passed=None, diff_lines=99)]
    assert select_best(cands) == 1


def test_smaller_diff_wins_when_tests_tie():
    cands = [_c(0, tests_passed=True, diff_lines=50), _c(1, tests_passed=True, diff_lines=5)]
    assert select_best(cands) == 1


def test_lower_index_breaks_full_tie():
    cands = [_c(0, tests_passed=True, diff_lines=10), _c(1, tests_passed=True, diff_lines=10)]
    assert select_best(cands) == 0


def test_all_nonviable_still_returns_an_index():
    cands = [_c(0, succeeded=False), _c(1, changed=False)]
    assert select_best(cands) in (0, 1)
    assert any_viable(cands) is False


def test_any_viable_true_when_one_changed_and_succeeded():
    assert any_viable([_c(0, succeeded=False), _c(1)]) is True


@pytest.mark.asyncio
async def test_candidate_tests_pass_reaps_process(tmp_path: Path):
    from orchestrator.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    orch._best_of_n_test_command = "python3 -c 'import time; time.sleep(0.01)'"

    wt = Worktree(base=str(tmp_path), path=str(tmp_path), branch="test-br", base_sha="sha")
    result = await orch._candidate_tests_pass(wt)
    assert result is True


@pytest.mark.asyncio
async def test_candidate_tests_pass_handles_timeout_and_kills_proc(tmp_path: Path):
    import orchestrator.orchestrator as orch_mod
    from orchestrator.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    # Simulate a command that takes too long
    orch._best_of_n_test_command = "python3 -c 'import time; time.sleep(10)'"

    wt = Worktree(base=str(tmp_path), path=str(tmp_path), branch="test-br", base_sha="sha")

    # Temporarily set timeout low for test
    orig_timeout = orch_mod._BEST_OF_N_TEST_TIMEOUT
    orch_mod._BEST_OF_N_TEST_TIMEOUT = 0.05
    try:
        result = await orch._candidate_tests_pass(wt)
        assert result is False
    finally:
        orch_mod._BEST_OF_N_TEST_TIMEOUT = orig_timeout
