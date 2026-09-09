"""Tests for learned approval memory, and for what counts as "the same action".

North replays a remembered decision instead of asking, so the fingerprint is a
safety boundary: two actions sharing one fingerprint means approving the first
silently approves the second. It used to hash only the first 80 characters of
the card message, which a `cd`-and-activate preamble exceeds on its own.
"""

from __future__ import annotations

import sqlite3

import pytest

from approval.approval_memory import (
    _DISPLAY_SIGNATURE_CHARS,
    ApprovalMemory,
    _display_signature,
    _fingerprint,
    _normalize,
)

# Longer than the old 80-character signature, as a real command prefix is.
LONG_PREFIX = "cd /Users/someone/Desktop/projects/north/services/backend/workers && source .venv/bin/activate && "


@pytest.fixture
def memory(tmp_path) -> ApprovalMemory:
    return ApprovalMemory(tmp_path / "approval_memory.db")


# ── What counts as the same action ───────────────────────────────────────────


def test_a_long_shared_prefix_no_longer_collides() -> None:
    """The bug: running the tests and deleting the data directory were one action."""
    assert len(LONG_PREFIX) > _DISPLAY_SIGNATURE_CHARS

    tests = _fingerprint("bash", LONG_PREFIX + "pytest tests/unit")
    destroy = _fingerprint("bash", LONG_PREFIX + "rm -rf ~/.north")

    assert tests != destroy


def test_approving_one_command_does_not_approve_its_long_prefixed_sibling(memory: ApprovalMemory) -> None:
    memory.record("bash", LONG_PREFIX + "pytest tests/unit", "approved")

    assert memory.recall("bash", LONG_PREFIX + "pytest tests/unit") == "approved"
    assert memory.recall("bash", LONG_PREFIX + "rm -rf ~/.north") is None


def test_the_same_action_still_matches_itself(memory: ApprovalMemory) -> None:
    """Narrowing identity must not break the feature it exists for."""
    memory.record("bash", "pytest tests/unit", "approved")

    assert memory.recall("bash", "pytest tests/unit") == "approved"


def test_formatting_noise_does_not_change_identity() -> None:
    """A card wraps its command in a fence; the fence is not part of the action."""
    assert _fingerprint("bash", "```\npytest tests/unit\n```") == _fingerprint("bash", "pytest    tests/unit")
    assert _fingerprint("bash", "PYTEST tests/unit") == _fingerprint("bash", "pytest tests/unit")


def test_the_same_command_from_a_different_agent_is_a_different_action() -> None:
    assert _fingerprint("bash", "git push") != _fingerprint("shell", "git push")


def test_a_differing_tail_makes_an_action_unique() -> None:
    """Being asked again is the safe way to be wrong; acting unasked is not."""
    a = _fingerprint("patch_file", "Apply this change to x.py?\n```diff\n-old line\n```")
    b = _fingerprint("patch_file", "Apply this change to x.py?\n```diff\n-other line\n```")

    assert a != b


# ── The stored label ─────────────────────────────────────────────────────────


def test_the_label_stays_short_for_the_cockpit(memory: ApprovalMemory) -> None:
    memory.record("bash", LONG_PREFIX + "pytest tests/unit", "approved")

    signature = memory.all_decisions()[0]["signature"]
    assert len(signature) <= _DISPLAY_SIGNATURE_CHARS


def test_the_label_is_truncated_but_identity_is_not() -> None:
    message = LONG_PREFIX + "pytest tests/unit"

    assert len(_display_signature(message)) < len(_normalize(message))


# ── Recording and forgetting ─────────────────────────────────────────────────


def test_only_clear_verdicts_are_learned(memory: ApprovalMemory) -> None:
    memory.record("bash", "pytest", "timeout_rejected")

    assert memory.recall("bash", "pytest") is None


def test_the_latest_verdict_wins_and_counts_up(memory: ApprovalMemory) -> None:
    memory.record("bash", "pytest", "approved")
    memory.record("bash", "pytest", "rejected")

    row = memory.all_decisions()[0]
    assert row["decision"] == "rejected"
    assert row["count"] == 2


def test_a_decision_can_be_withdrawn(memory: ApprovalMemory) -> None:
    memory.record("bash", "pytest", "approved")
    fingerprint = memory.all_decisions()[0]["fingerprint"]

    assert memory.forget(fingerprint)
    assert memory.recall("bash", "pytest") is None


# ── The one-time purge of prefix-keyed rows ──────────────────────────────────


def _write_legacy_row(db_path) -> None:
    """A row as the old build left it: keyed by a hash we can no longer re-derive."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS approval_decisions ("
            "fingerprint TEXT NOT NULL PRIMARY KEY, agent TEXT NOT NULL, signature TEXT NOT NULL, "
            "decision TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO approval_decisions (fingerprint, agent, signature, decision) VALUES (?,?,?,?)",
            ("stale-prefix-hash", "bash", "pytest tests/unit", "approved"),
        )


def test_decisions_keyed_by_the_old_prefix_hash_are_dropped(tmp_path) -> None:
    db_path = tmp_path / "approval_memory.db"
    _write_legacy_row(db_path)

    memory = ApprovalMemory(db_path)

    assert memory.all_decisions() == [], "a verdict that may not be the user's must not be replayed"


def test_the_purge_happens_only_once(tmp_path) -> None:
    """A second start must not wipe decisions made under the new fingerprint."""
    db_path = tmp_path / "approval_memory.db"
    _write_legacy_row(db_path)
    ApprovalMemory(db_path).record("bash", "pytest tests/unit", "approved")

    reopened = ApprovalMemory(db_path)

    assert reopened.recall("bash", "pytest tests/unit") == "approved"


def test_a_fresh_database_is_unaffected(tmp_path) -> None:
    memory = ApprovalMemory(tmp_path / "fresh.db")
    memory.record("bash", "pytest", "approved")

    assert ApprovalMemory(tmp_path / "fresh.db").recall("bash", "pytest") == "approved"
