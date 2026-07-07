"""Tests for ApprovalMemory (learned approvals)."""

from __future__ import annotations

from pathlib import Path

from approval.approval_memory import ApprovalMemory


def test_record_and_recall(tmp_path: Path):
    m = ApprovalMemory(tmp_path / "am.db")
    m.record("bash", "```\nnpm install left-pad\n```", "approved")
    assert m.recall("bash", "```\nnpm install left-pad\n```") == "approved"


def test_recall_normalizes_fences_and_whitespace(tmp_path: Path):
    m = ApprovalMemory(tmp_path / "am.db")
    m.record("bash", "```bash\nnpm   install   x\n```", "approved")
    # different fence/whitespace, same command -> same fingerprint
    assert m.recall("bash", "npm install x") == "approved"


def test_unknown_recall_is_none(tmp_path: Path):
    m = ApprovalMemory(tmp_path / "am.db")
    assert m.recall("bash", "anything") is None


def test_latest_decision_wins(tmp_path: Path):
    m = ApprovalMemory(tmp_path / "am.db")
    m.record("git", "git commit -m x", "approved")
    m.record("git", "git commit -m x", "rejected")
    assert m.recall("git", "git commit -m x") == "rejected"


def test_only_learns_approve_or_reject(tmp_path: Path):
    m = ApprovalMemory(tmp_path / "am.db")
    m.record("bash", "cmd", "timeout_rejected")
    assert m.recall("bash", "cmd") is None


def test_persists_across_instances(tmp_path: Path):
    db = tmp_path / "am.db"
    ApprovalMemory(db).record("bash", "pytest -q", "approved")
    assert ApprovalMemory(db).recall("bash", "pytest -q") == "approved"


def test_agent_scopes_the_fingerprint(tmp_path: Path):
    m = ApprovalMemory(tmp_path / "am.db")
    m.record("bash", "do thing", "approved")
    assert m.recall("git", "do thing") is None
