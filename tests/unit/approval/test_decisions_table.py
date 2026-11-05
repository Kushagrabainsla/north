"""One decisions table: what you decided and why, once (#32).

`approval_decisions` (replay by action) and `card_decisions` (prepared work and
the reason for a rejection) recorded the same thing twice. Now every decision of
yours is one row that replay and flow learning both read.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from approval.approval_memory import ApprovalMemory
from approval.models import ApprovalDecision, Card, CardField, CardType


def _rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    finally:
        conn.close()


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def _prepared_work() -> Card:
    return Card.new(
        type=CardType.APPROVAL,
        agent="job",
        title="Apply to Acme?",
        message="Submit it?",
        source="job_applications",
        blocking=False,
        fields=[CardField(name="company", value="Acme")],
        action_key="job other cmd=Submit it?",
    )


def test_one_decision_is_one_row_that_replay_reads(tmp_path: Path) -> None:
    db = tmp_path / "approval_memory.db"
    memory = ApprovalMemory(db)
    card = _prepared_work()

    memory.record(card.agent, card.action_key, ApprovalDecision.REJECTED, card=card, reason="wrong city")

    [row] = _rows(db)
    assert (row["card_id"], row["decision"], row["reason"], row["decided_by"]) == (
        card.id,
        "rejected",
        "wrong city",
        "you",
    )
    assert memory.recall(card.agent, card.action_key) == "rejected"


def test_your_latest_answer_replays_and_count_is_how_often_you_answered(tmp_path: Path) -> None:
    memory = ApprovalMemory(tmp_path / "m.db")
    for decision in ("approved", "approved", "rejected"):
        memory.record("bash", "bash shell_command cmd=make deploy", decision)

    [learned] = memory.all_decisions()

    assert (learned["decision"], learned["count"]) == ("rejected", 3)
    assert memory.recall("bash", "bash shell_command cmd=make deploy") == "rejected"
    assert ApprovalMemory(tmp_path / "m.db").recall("bash", "bash shell_command cmd=make deploy") == "rejected"


def test_an_answered_question_is_recorded_but_never_replayed(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    memory = ApprovalMemory(db)

    memory.record("coder", "", ApprovalDecision.ANSWERED, chosen_option="Postgres")

    assert _rows(db)[0]["chosen_option"] == "Postgres"
    assert memory.all_decisions() == []


def test_forgetting_stops_the_replay_but_keeps_what_you_decided(tmp_path: Path) -> None:
    db = tmp_path / "approval_memory.db"
    memory = ApprovalMemory(db)
    card = _prepared_work()
    memory.record(card.agent, card.action_key, "approved", card=card)
    [learned] = memory.all_decisions()

    assert memory.forget(learned["fingerprint"])

    assert memory.recall(card.agent, card.action_key) is None
    assert ApprovalMemory(db).recall(card.agent, card.action_key) is None
    assert [row["decision"] for row in _rows(db)] == ["approved"], "what you decided is kept"


def _old_database(db: Path) -> None:
    """A database from before #32, whose one-time purges already ran."""
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE approval_memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO approval_memory_meta VALUES ('prefix_fingerprints_dropped', '1'),
                                                ('message_keyed_decisions_dropped', '1');
        CREATE TABLE approval_decisions (fingerprint TEXT PRIMARY KEY, agent TEXT, signature TEXT,
                                         decision TEXT, count INTEGER, updated_at DATETIME);
        CREATE TABLE card_decisions (card_id TEXT PRIMARY KEY, source TEXT, agent TEXT, decision TEXT,
                                     reason TEXT, title TEXT, summary TEXT, edited_fields TEXT, decided_at DATETIME);
        INSERT INTO card_decisions VALUES ('c1', 'job_applications', 'job', 'rejected', 'too junior',
                                           'Junior dev', 'Junior dev at Co', '[]', '2026-09-01T00:00:00');
        """
    )
    conn.commit()
    conn.close()


def test_the_old_tables_move_into_the_new_one(tmp_path: Path) -> None:
    db = tmp_path / "approval_memory.db"
    _old_database(db)
    memory_key = "bash shell_command cmd=pytest"
    # A replay row, fingerprinted exactly as the old table stored it.
    from approval.approval_memory import _display_signature, _fingerprint

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO approval_decisions VALUES (?, 'bash', ?, 'approved', 2, '2026-09-02 00:00:00')",
        (_fingerprint("bash", memory_key), _display_signature(memory_key)),
    )
    conn.commit()
    conn.close()

    ApprovalMemory(db)

    assert {"approval_decisions", "card_decisions"}.isdisjoint(_tables(db))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT decision FROM decisions WHERE agent = 'bash'").fetchall() == [("approved",)]
    # Moved, but keyed by the old normalized identity: it no longer replays, north asks again.
    assert ApprovalMemory(db).recall("bash", memory_key) is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT reason FROM decisions WHERE card_source = 'job_applications'").fetchall() == [
            ("too junior",)
        ]
