"""Assertions for experimental recovery, not production feature tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from experiments.lean_trials.recovery_validation import attempt, claim, initialize, receive, sql
from experiments.lean_trials.sandbox_validation import environment, wheel


@pytest.mark.parametrize("arm", ["atomic_pause", "atomic_idempotent", "atomic_receipt"])
def test_payload_changes_cannot_reuse_an_operation_id(tmp_path, arm) -> None:
    initialize(tmp_path)
    assert attempt(tmp_path, arm, "none") == "succeeded"
    assert attempt(tmp_path, arm, "none", payload="account=B") == "payload_mismatch"
    assert sql(tmp_path / "receiver.db", "SELECT count(*) FROM effects")[0][0] == 1


@pytest.mark.parametrize("arm", ["atomic_pause", "atomic_idempotent", "atomic_receipt"])
def test_failed_claim_never_dispatches(tmp_path, arm) -> None:
    initialize(tmp_path)
    with (
        patch("experiments.lean_trials.recovery_validation.claim", side_effect=sqlite3.OperationalError("disk full")),
        pytest.raises(sqlite3.OperationalError),
    ):
        attempt(tmp_path, arm, "none")
    assert sql(tmp_path / "receiver.db", "SELECT count(*) FROM effects")[0][0] == 0


@pytest.mark.parametrize("arm", ["atomic_pause", "atomic_idempotent", "atomic_receipt"])
def test_separate_intents_with_identical_payloads_are_not_deduplicated(tmp_path, arm) -> None:
    initialize(tmp_path)
    attempt(tmp_path, arm, "none", op="one")
    attempt(tmp_path, arm, "none", op="two")
    assert sql(tmp_path / "receiver.db", "SELECT count(*) FROM effects")[0][0] == 2


def test_receipt_absence_does_not_mean_the_old_sender_has_stopped(tmp_path) -> None:
    initialize(tmp_path)
    assert claim(tmp_path / "intent.db", "operation-1", "account=A;amount=10", "atomic_receipt") == "dispatch"
    assert attempt(tmp_path, "atomic_receipt", "none") == "unknown"
    receive(tmp_path, "operation-1", "account=A;amount=10", dedup=False)
    assert attempt(tmp_path, "atomic_receipt", "none") == "succeeded"
    assert sql(tmp_path / "receiver.db", "SELECT count(*) FROM effects")[0][0] == 1


def test_known_pre_dispatch_failure_can_be_retried(tmp_path) -> None:
    initialize(tmp_path)
    assert attempt(tmp_path, "atomic_pause", "known_no_effect") == "known_no_effect"
    assert attempt(tmp_path, "atomic_pause", "none") == "succeeded"


def test_atomic_claim_has_one_initial_owner(tmp_path) -> None:
    initialize(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim(tmp_path / "intent.db", "same", "A", "atomic_pause"), range(8)))
    assert results.count("dispatch") == 1
    assert results.count("unknown") == 7


def test_environment_allowlist_scrubs_other_keys_without_changing_home(tmp_path, monkeypatch) -> None:
    cache, temporary = tmp_path / "cache", tmp_path / "temp"
    cache.mkdir()
    temporary.mkdir()
    monkeypatch.setenv("A_SYNTHETIC_PROVIDER_SECRET", "not-a-real-key")
    env = environment(cache, temporary, secret=False)
    assert "A_SYNTHETIC_PROVIDER_SECRET" not in env
    assert "NORTH_TRIAL_CREDENTIAL" not in env
    assert "HOME" not in env and "CODEX_HOME" not in env
    assert env["npm_config_userconfig"] != env["npm_config_globalconfig"]


def test_offline_wheel_is_a_valid_local_fixture(tmp_path) -> None:
    import zipfile

    path = tmp_path / "tiny_trial-1.0-py3-none-any.whl"
    wheel(path)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert archive.read("tiny_trial/__init__.py") == b"value = 42\n"
