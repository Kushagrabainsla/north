"""The cockpit's artifact library, now that it reads the handoff tree.

Handoff artifacts (research notes, specs, QA reports) live in
``<NORTH_HOME>/tasks/<task_id>/``. Listing that directory is what removed the
need for the researcher to copy its findings into the user's repo - but the
task-state database sits in the very same directory, so what the library must
*not* serve matters as much as what it must.
"""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from orchestrator.api_context import ApiServices, bind_services
from web import api as web_api


@pytest.fixture
def north_home(tmp_path):
    """A NORTH_HOME holding one personal output, one task's artifacts, and state."""
    (tmp_path / "news").mkdir()
    (tmp_path / "news" / "2026-01-01.md").write_text("# Briefing", encoding="utf-8")

    research = tmp_path / "tasks" / "task_abc123" / "research"
    research.mkdir(parents=True)
    (research / "context.md").write_text("## Task Summary\nfindings", encoding="utf-8")
    (research / "references.json").write_text("[]", encoding="utf-8")
    qa = tmp_path / "tasks" / "task_abc123" / "qa"
    qa.mkdir(parents=True)
    (qa / "review_report_latest.md").write_text("PASS", encoding="utf-8")

    # north's own state, sharing the directory with the artifacts.
    (tmp_path / "tasks" / "tasks.db").write_bytes(b"SQLite format 3\x00")
    (tmp_path / "tasks" / "tasks.db-wal").write_bytes(b"wal")
    (tmp_path / "secret.key").write_text("s3cret", encoding="utf-8")
    (tmp_path / ".env").write_text("NORTH_OPENROUTER_API_KEY=sk-real", encoding="utf-8")

    with bind_services(ApiServices(north_home=tmp_path)):
        yield tmp_path


def _id_for(relative: str) -> str:
    return base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")


async def test_handoff_artifacts_are_listed_with_their_task(north_home) -> None:
    listed = await web_api.list_artifacts()
    by_name = {row["name"]: row for row in listed}

    assert by_name["context.md"]["task"] == "task_abc123"
    assert by_name["context.md"]["kind"] == "research"
    assert by_name["review_report_latest.md"]["kind"] == "qa"
    # A personal output belongs to no task, so the UI can keep it out of the runs.
    assert by_name["2026-01-01.md"]["task"] == ""


async def test_state_files_are_never_listed_as_artifacts(north_home) -> None:
    """`tasks.db` lives beside the handoff directories it must not be listed with."""
    names = {row["name"] for row in await web_api.list_artifacts()}
    assert names == {"context.md", "references.json", "review_report_latest.md", "2026-01-01.md"}


@pytest.mark.parametrize(
    "relative",
    [
        "tasks/tasks.db",
        "tasks/tasks.db-wal",
        "secret.key",
        ".env",
        "tasks/../secret.key",
        "../.ssh/id_rsa",
    ],
)
async def test_forged_ids_cannot_read_outside_the_library(north_home, relative: str) -> None:
    """The id is client-supplied, so containment is enforced on the way out too."""
    with pytest.raises(HTTPException) as excinfo:
        await web_api.get_artifact(_id_for(relative))
    assert excinfo.value.status_code == 404


async def test_a_handoff_artifact_reads_back_with_its_task(north_home) -> None:
    artifact = await web_api.get_artifact(_id_for("tasks/task_abc123/research/context.md"))
    assert artifact["task"] == "task_abc123"
    assert artifact["kind"] == "research"
    assert "findings" in artifact["content"]
