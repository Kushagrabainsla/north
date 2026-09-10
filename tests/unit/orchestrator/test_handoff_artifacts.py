from __future__ import annotations

from orchestrator.handoff_artifacts import read_artifact


def test_read_artifact_returns_none_for_missing_or_empty_paths(tmp_path) -> None:
    assert read_artifact(None, 10) is None
    assert read_artifact(tmp_path / "missing.md", 10) is None

    empty = tmp_path / "empty.md"
    empty.write_text(" \n", encoding="utf-8")
    assert read_artifact(empty, 10) is None


def test_read_artifact_strips_and_caps_content(tmp_path) -> None:
    artifact = tmp_path / "artifact.md"
    artifact.write_text("  abcdef  ", encoding="utf-8")

    assert read_artifact(artifact, 3) == "abc\n[…3 chars truncated]"
