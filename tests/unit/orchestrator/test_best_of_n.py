"""Tests for best-of-N candidate selection (#11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.best_of_n import CandidateOutcome, any_viable, select_best
from orchestrator.isolation import AgentIsolation
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


def _isolation(test_command: str) -> AgentIsolation:
    """Just the isolation collaborator - no orchestrator needed to test it."""
    return AgentIsolation(
        enabled=True,
        worktree_root="",
        best_of_n=1,
        test_command=test_command,
        stream_manager=None,
        write_ledger=None,
        run_agent=None,
    )


@pytest.mark.asyncio
async def test_candidate_tests_pass_reaps_process(tmp_path: Path):
    isolation = _isolation("python3 -c 'import time; time.sleep(0.01)'")
    wt = Worktree(base=str(tmp_path), path=str(tmp_path), branch="test-br", base_sha="sha")
    assert await isolation._candidate_tests_pass(wt) is True


@pytest.mark.asyncio
async def test_candidate_tests_pass_handles_timeout_and_kills_proc(tmp_path: Path):
    import orchestrator.isolation as isolation_mod

    isolation = _isolation("python3 -c 'import time; time.sleep(10)'")
    wt = Worktree(base=str(tmp_path), path=str(tmp_path), branch="test-br", base_sha="sha")

    original = isolation_mod._BEST_OF_N_TEST_TIMEOUT
    isolation_mod._BEST_OF_N_TEST_TIMEOUT = 0.05
    try:
        assert await isolation._candidate_tests_pass(wt) is False
    finally:
        isolation_mod._BEST_OF_N_TEST_TIMEOUT = original
