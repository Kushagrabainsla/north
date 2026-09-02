"""Tests for fix 2c: the Definition-of-Done evaluator (orchestrator/dod.py)
and its warn-only wiring in the orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from approval.store import ApprovalStore
from ledger.models import LedgerEntry, LedgerSource
from orchestrator.dod import evaluate_engineering_dod
from orchestrator.orchestrator import Orchestrator
from orchestrator.review import ReviewResult

# ---------------------------------------------------------------- pure evaluator

_PASS_REVIEW = ReviewResult.parse({"status": "PASS", "must_fix": [], "tests": {"passed": True}})


def test_dod_passes_with_all_evidence():
    r = evaluate_engineering_dod(
        change_applied=True,
        coder_models=["model-a"],
        reviewer_models=["model-b"],
        review=_PASS_REVIEW,
    )
    assert r.passed is True
    assert r.reasons == []


def test_dod_fails_without_change():
    r = evaluate_engineering_dod(
        change_applied=False, coder_models=["a"], reviewer_models=["b"], review=_PASS_REVIEW
    )
    assert r.passed is False
    assert any("no code change" in x for x in r.reasons)


def test_dod_fails_without_review():
    r = evaluate_engineering_dod(
        change_applied=True, coder_models=["a"], reviewer_models=["b"], review=None
    )
    assert r.passed is False
    assert any("no structured review" in x for x in r.reasons)


def test_dod_fails_when_review_has_must_fix():
    review = ReviewResult.parse({"status": "FAIL", "must_fix": ["foo.py:1 - bug"]})
    r = evaluate_engineering_dod(change_applied=True, coder_models=["a"], reviewer_models=["b"], review=review)
    assert r.passed is False
    assert any("must-fix" in x for x in r.reasons)


def test_dod_fails_on_same_model_review():
    # The core rubber-duck guarantee: reviewer must differ from coder.
    r = evaluate_engineering_dod(
        change_applied=True, coder_models=["model-a"], reviewer_models=["model-a"], review=_PASS_REVIEW
    )
    assert r.passed is False
    assert any("same model as the coder" in x for x in r.reasons)


def test_dod_fails_when_reviewer_model_unknown():
    r = evaluate_engineering_dod(
        change_applied=True, coder_models=["a"], reviewer_models=[], review=_PASS_REVIEW
    )
    assert r.passed is False
    assert any("reviewer model was not recorded" in x for x in r.reasons)


def test_dod_fails_when_coder_model_unknown():
    r = evaluate_engineering_dod(
        change_applied=True, coder_models=[], reviewer_models=["b"], review=_PASS_REVIEW
    )
    assert r.passed is False
    assert any("coder model was not recorded" in x for x in r.reasons)


def test_dod_fails_when_review_tests_failed():
    review = ReviewResult.parse({"status": "PASS", "must_fix": [], "tests": {"passed": False}})
    r = evaluate_engineering_dod(change_applied=True, coder_models=["a"], reviewer_models=["b"], review=review)
    assert r.passed is False


# ---------------------------------------------------------------- bugfix evidence (B1)


def _passing_bugfix(verification: dict) -> ReviewResult:
    return ReviewResult.parse(
        {"status": "PASS", "must_fix": [], "tests": {"passed": True}, "verification": verification}
    )


def _eval_bugfix(review: ReviewResult, kind: str = "bugfix"):
    return evaluate_engineering_dod(
        change_applied=True, coder_models=["a"], reviewer_models=["b"], review=review, kind=kind
    )


def test_bugfix_passes_when_evidence_absent_fail_open():
    # Conservative + fail-open: a bugfix with NO recorded verification evidence still
    # passes on the base checks - absence is never held against the task.
    assert _eval_bugfix(_passing_bugfix({})).passed is True


def test_bugfix_passes_with_full_positive_evidence():
    r = _eval_bugfix(
        _passing_bugfix(
            {
                "reproduction_command": "pytest tests/test_bug.py::test_x",
                "pre_fix_failed": True,
                "post_fix_passed": True,
                "regression_test_added": True,
                "regression_test_path": "tests/test_bug.py",
            }
        )
    )
    assert r.passed is True
    assert r.reasons == []


def test_bugfix_fails_when_reproduction_still_fails_post_fix():
    r = _eval_bugfix(_passing_bugfix({"post_fix_passed": False}))
    assert r.passed is False
    assert any("did not pass after the fix" in x for x in r.reasons)


def test_bugfix_fails_when_bug_never_reproduced():
    r = _eval_bugfix(_passing_bugfix({"reproduction_command": "pytest x", "pre_fix_failed": False}))
    assert r.passed is False
    assert any("not reproduced before fixing" in x for x in r.reasons)


def test_bugfix_fails_when_regression_test_explicitly_absent():
    r = _eval_bugfix(_passing_bugfix({"regression_test_added": False}))
    assert r.passed is False
    assert any("no regression test" in x for x in r.reasons)


def test_bugfix_evidence_ignored_for_non_bug_kinds():
    # A feature/refactor task must not be judged on bugfix verification fields, even
    # if a stray False slips in - the checks are gated on the kind.
    review = _passing_bugfix({"regression_test_added": False, "post_fix_passed": False})
    assert _eval_bugfix(review, kind="feature").passed is True
    assert _eval_bugfix(review, kind="").passed is True


# ---------------------------------------------------------------- auto-verify oracle (B2)


def test_dod_fails_when_auto_verify_fails():
    r = evaluate_engineering_dod(
        change_applied=True,
        coder_models=["a"],
        reviewer_models=["b"],
        review=_PASS_REVIEW,
        auto_verify_passed=False,
    )
    assert r.passed is False
    assert any("independent verification failed" in x for x in r.reasons)


def test_dod_passes_when_auto_verify_passes():
    r = evaluate_engineering_dod(
        change_applied=True,
        coder_models=["a"],
        reviewer_models=["b"],
        review=_PASS_REVIEW,
        auto_verify_passed=True,
    )
    assert r.passed is True


def test_dod_fail_open_when_auto_verify_unknown():
    # None = no command detected / harness couldn't run: never held against the task.
    r = evaluate_engineering_dod(
        change_applied=True,
        coder_models=["a"],
        reviewer_models=["b"],
        review=_PASS_REVIEW,
        auto_verify_passed=None,
    )
    assert r.passed is True


def test_resolve_verify_command_prefers_explicit_setting(tmp_path):
    orch, _, _ = _orchestrator([])
    orch._verify_command = "make check"
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    # The explicit setting wins over auto-detection.
    assert orch._resolve_verify_command(str(tmp_path)) == "make check"


def test_resolve_verify_command_falls_back_to_detection(tmp_path):
    orch, _, _ = _orchestrator([])
    orch._verify_command = ""
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert orch._resolve_verify_command(str(tmp_path)) == "go test ./..."


async def test_run_verify_command_reports_exit_code(tmp_path):
    orch, _, _ = _orchestrator([])
    assert await orch._run_verify_command(str(tmp_path), "true") is True
    assert await orch._run_verify_command(str(tmp_path), "false") is False


async def test_run_verify_command_not_found_is_fail_open(tmp_path):
    # A missing runner (exit 127) must be "unknown" (None), never a failure - so an
    # uninstalled pytest never fails the DoD. Regression for a live-found bug.
    orch, _, _ = _orchestrator([])
    assert await orch._run_verify_command(str(tmp_path), "definitely-not-a-real-command-xyz -q") is None


async def test_auto_verify_records_and_returns(tmp_path, monkeypatch):
    orch, ledger, stream = _orchestrator([])
    orch._verify_command = "true"
    result = await orch._auto_verify("t1", str(tmp_path))
    assert result is True
    ledger.write.assert_awaited()
    assert any(c.args and c.args[1] == "auto_verify" for c in stream.emit.await_args_list)


async def test_auto_verify_no_command_is_noop(tmp_path):
    orch, ledger, _ = _orchestrator([])
    orch._verify_command = ""  # and tmp_path has no project markers
    assert await orch._auto_verify("t1", str(tmp_path)) is None
    ledger.write.assert_not_called()



def _orchestrator(entries: list[LedgerEntry]):
    ledger = MagicMock()
    ledger.query_summaries = AsyncMock(return_value=entries)
    ledger.write = AsyncMock()
    stream_manager = MagicMock()
    stream_manager.emit = AsyncMock()
    stream_manager.emit_done = AsyncMock()
    orch = Orchestrator(
        ledger=ledger,
        agent_registry=MagicMock(),
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=MagicMock(),
        failure_handler=MagicMock(),
        notifier=MagicMock(),
        stream_manager=stream_manager,
        approval_store=ApprovalStore(),
    )
    return orch, ledger, stream_manager


def _completed(agent: str, model: str, tools: list[str]) -> LedgerEntry:
    return LedgerEntry.new(
        source=LedgerSource.AGENT, task_id="t1", agent=agent, action="agent_completed",
        model_used=model, tools_used=tools,
    )


async def test_warn_only_skips_non_engineering():
    orch, ledger, _ = _orchestrator([])
    await orch._evaluate_dod("t1", "general")
    ledger.query_summaries.assert_not_called()
    ledger.write.assert_not_called()


async def test_warn_only_records_verdict_and_never_blocks(tmp_path, monkeypatch):
    # No review file → DoD unmet, but the method must only record, never raise.
    from orchestrator import review as review_mod

    monkeypatch.setattr(review_mod, "handoff_dir_for", lambda tid: str(tmp_path / tid))
    entries = [
        _completed("coder", "model-a", ["patch_file"]),
        _completed("reviewer", "model-b", ["bash"]),
    ]
    orch, ledger, stream = _orchestrator(entries)
    await orch._evaluate_dod("t1", "engineering")

    dod_writes = [c.args[0] for c in ledger.write.call_args_list if c.args[0].action == "dod_evaluated"]
    assert len(dod_writes) == 1
    assert dod_writes[0].error_type == "dod_unmet"  # no review verdict present
    stream.emit.assert_awaited()


async def test_warn_only_evaluation_error_is_swallowed():
    orch, ledger, _ = _orchestrator([])
    ledger.query_summaries = AsyncMock(side_effect=RuntimeError("db down"))
    # Must not raise - DoD warn-only fails open.
    await orch._evaluate_dod("t1", "engineering")


# ---------------------------------------------------------------- DoD enforcement in _finish_task


def _terminal(ledger):
    terminal_actions = {"task_completed", "task_completed_with_failures", "task_failed"}
    for call in ledger.write.call_args_list:
        entry = call.args[0]
        if entry.action in terminal_actions:
            return entry
    raise AssertionError("no terminal ledger entry written")


async def test_finish_task_dod_unmet_marks_completed_with_failures():
    orch, ledger, stream = _orchestrator([])
    await orch._finish_task("t1", failures=[], total_agents=2, dod_unmet_reasons=["review used the same model"])
    entry = _terminal(ledger)
    assert entry.action == "task_completed_with_failures"
    assert entry.error_type == "dod_unmet"
    assert "Definition of Done not met" in (entry.output or "")
    # The stream event carries the reasons for the UI.
    completed = [c for c in stream.emit.call_args_list if c.args[1] == "task_completed"]
    assert completed and completed[0].args[2]["dod_unmet"] == ["review used the same model"]


async def test_finish_task_clean_when_dod_met():
    orch, ledger, _ = _orchestrator([])
    await orch._finish_task("t1", failures=[], total_agents=2, dod_unmet_reasons=None)
    entry = _terminal(ledger)
    assert entry.action == "task_completed"
    assert entry.error_type is None


async def test_finish_task_all_agents_failed_takes_precedence_over_dod():
    orch, ledger, _ = _orchestrator([])
    await orch._finish_task("t1", failures=["coder", "reviewer"], total_agents=2, dod_unmet_reasons=["x"])
    entry = _terminal(ledger)
    assert entry.action == "task_failed"
    assert entry.error_type == "agent_failure"
