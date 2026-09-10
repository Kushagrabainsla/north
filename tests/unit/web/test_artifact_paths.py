from __future__ import annotations

from web.artifacts import (
    allowed_output_roots,
    artifact_task_id,
    encode_artifact_id,
    is_readable_artifact,
    resolve_artifact,
)


def test_allowed_roots_are_scoped_to_known_output_directories(tmp_path) -> None:
    assert allowed_output_roots(tmp_path) == [tmp_path / name for name in ("news", "notes", "wellness", "tasks")]


def test_state_databases_are_never_readable_artifacts(tmp_path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    readable = notes / "briefing.md"
    readable.write_text("hello", encoding="utf-8")
    state = notes / "tasks.db"
    state.write_text("x", encoding="utf-8")

    assert is_readable_artifact(readable)
    assert not is_readable_artifact(state)
    assert not is_readable_artifact(notes)


def test_task_attribution_only_applies_inside_the_task_tree(tmp_path) -> None:
    assert artifact_task_id(tmp_path / "tasks" / "t1" / "spec.md", tmp_path) == "t1"
    assert artifact_task_id(tmp_path / "notes" / "note.md", tmp_path) == ""
    assert artifact_task_id(tmp_path.parent / "elsewhere.md", tmp_path) == ""


def test_identifiers_round_trip_and_reject_escapes(tmp_path) -> None:
    artifact = tmp_path / "tasks" / "t1" / "spec.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("spec", encoding="utf-8")

    identifier = encode_artifact_id(artifact, tmp_path)
    assert resolve_artifact(identifier, tmp_path) == artifact.resolve()

    outside = tmp_path / "secret.txt"
    outside.write_text("no", encoding="utf-8")
    assert resolve_artifact(encode_artifact_id(outside, tmp_path), tmp_path) is None
    assert resolve_artifact("not-base64-@@", tmp_path) is None
