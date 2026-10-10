"""Check trial controls and reporting; these are not production feature tests."""

from __future__ import annotations

from experiments.lean_trials.reviewers import CASES, summary, validate_cases
from experiments.lean_trials.trials import (
    capability_evidence,
    checklist,
    delivery,
    loopback,
    permissions,
    scheduling,
)


async def test_permission_trial_includes_authorized_and_unauthorized_work() -> None:
    results = await permissions()
    rows = results["rows"]
    assert any(r["expected"] for r in rows)
    assert any(not r["expected"] for r in rows)
    # Conservative modes must report lost usefulness, not just zero unsafe acts.
    assert results["summary"]["global_ask"]["extra_prompt_or_block_cases"] > 0
    assert results["summary"]["global_yolo"]["unauthorized"] > 0


def test_restart_and_stale_evidence_are_distinct(tmp_path) -> None:
    results = checklist(tmp_path)
    rows = results["rows"]
    assert next(r for r in rows if r["arm"] == "current_todo" and r["case"] == "restart")["expected"]
    assert not next(r for r in rows if r["arm"] == "persisted_ticks" and r["case"] == "old_attempt")["expected"]
    assert next(r for r in rows if r["arm"] == "persisted_ticks" and r["case"] == "old_attempt")["ready"]


def test_outbox_reports_lost_ack_duplicates(tmp_path) -> None:
    results = delivery(tmp_path)
    assert results["summary"]["outbox"]["lost_cases"] == 0
    assert results["summary"]["outbox"]["duplicate_cases"] > 0


def test_existing_capability_fingerprint_has_both_change_and_no_change_controls(tmp_path) -> None:
    results = capability_evidence(tmp_path)
    assert any(not r["expected"] for r in results["rows"])
    # Current hashes cover definition changes, but not a referenced file's bytes.
    assert results["summary"]["current_fingerprint"]["stale_evidence_accepted"] == 1
    assert results["summary"]["current_fingerprint"]["needless_invalidations"] == 0
    assert results["summary"]["fingerprint_all_bundled_files"]["stale_evidence_accepted"] == 0
    assert results["summary"]["fingerprint_all_bundled_files"]["needless_invalidations"] == 1


def test_loopback_guard_includes_ipv6_and_host_name_false_positive() -> None:
    results = loopback()
    assert results["summary"]["literal_ip_only"]["legitimate_rejected"] == 1
    assert results["summary"]["literal_ip_or_localhost"]["unsafe_accepted"] == 0


def test_every_simulation_pair_has_exactly_one_win_loss_or_tie() -> None:
    results = scheduling()
    assert len(results["rows"]) == 600
    for comparison in results["paired_comparisons"]:
        for metric in ("fg_mean", "fg_p95", "bg_mean", "drain_ticks"):
            counts = comparison[metric]
            assert counts["wins"] + counts["ties"] + counts["losses"] == comparison["scenarios"] == 200


def test_live_labels_are_executable_and_balanced() -> None:
    validate_cases()
    assert sum(case["bug"] for case in CASES) == len(CASES) / 2


def test_unavailable_model_is_not_scored_as_a_wrong_answer() -> None:
    results = summary([{"strategy": "invariant", "error": "rate limited"}])
    assert results["invariant"]["completed"] == 0
    assert results["invariant"]["errors"] == 1
    assert results["paired_accuracy"]["pairs"] == 0
