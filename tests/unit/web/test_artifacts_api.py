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
    # A file written outside any task is not an artifact of anything.
    assert "2026-01-01.md" not in by_name


async def test_state_files_are_never_listed_as_artifacts(north_home) -> None:
    """`tasks.db` lives beside the handoff directories it must not be listed with."""
    names = {row["name"] for row in await web_api.list_artifacts()}
    assert names == {"context.md", "references.json", "review_report_latest.md"}


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


async def test_each_task_group_is_named_by_its_flow_session_or_request(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    for task in ("flowtask", "chattask", "task_plain0000"):
        (tmp_path / "tasks" / task).mkdir(parents=True)
        (tmp_path / "tasks" / task / "out.md").write_text("x", encoding="utf-8")
    flows = Mock(flow_names_for_tasks=Mock(return_value={"flowtask": "daily-news-briefing"}))
    sessions = Mock(titles_for_tasks=AsyncMock(return_value={"chattask": ("conv1", "Trip planning")}))
    received = SimpleNamespace(action="task_received", input="Summarise my week\nwith detail")
    ledger = Mock(query=AsyncMock(return_value=[received]))
    services = ApiServices(north_home=tmp_path, flow_store=flows, conversation_store=sessions, ledger=ledger)

    with bind_services(services):
        owners = {row["task"]: row["owner"] for row in await web_api.list_artifacts()}

    assert owners["flowtask"] == {"kind": "flow", "name": "daily-news-briefing", "link": "/flows"}
    assert owners["chattask"] == {"kind": "session", "name": "Trip planning", "link": "/sessions/conv1"}
    assert owners["task_plain0000"]["name"] == "Summarise my week"
    assert owners["task_plain0000"]["link"] == "/tasks/task_plain0000"


async def test_a_task_folder_inside_a_store_names_the_store_as_its_kind(tmp_path) -> None:
    research = tmp_path / "news" / "research" / "flow_a76e4e08e204"
    research.mkdir(parents=True)
    (research / "2026-10-13.md").write_text("papers", encoding="utf-8")

    with bind_services(ApiServices(north_home=tmp_path)):
        [row] = await web_api.list_artifacts()

    assert (row["task"], row["kind"]) == ("flow_a76e4e08e204", "research")


async def test_a_recording_is_served_as_itself_not_read_as_text(north_home) -> None:
    """Issue #81: a 5-second camera recording opened as a screen of bytes; the page now plays it from `/file`."""
    recording = north_home / "tasks" / "task_abc123" / "camera-5s.mov"
    recording.write_bytes(b"\x00\x00\x00\x14ftypqt  \xff\xfe" + b"\x00" * 3_000_000)
    artifact_id = _id_for("tasks/task_abc123/camera-5s.mov")

    preview = await web_api.get_artifact(artifact_id)
    served = await web_api.get_artifact_file(artifact_id)
    [listed] = [row for row in await web_api.list_artifacts() if row["name"] == "camera-5s.mov"]

    assert preview["media_type"] == listed["media_type"] == served.media_type == "video/quicktime"
    assert preview["content"] == "", "not read as text, and its 3 MB are no reason to refuse it"
    assert str(served.path) == str(recording)


async def test_a_text_artifact_too_large_to_preview_is_still_refused(north_home) -> None:
    (north_home / "tasks" / "task_abc123" / "huge.md").write_text("x" * 2_000_001, encoding="utf-8")

    with pytest.raises(HTTPException) as excinfo:
        await web_api.get_artifact(_id_for("tasks/task_abc123/huge.md"))
    assert excinfo.value.status_code == 413


async def test_the_file_route_cannot_serve_outside_the_library(north_home) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await web_api.get_artifact_file(_id_for("secret.key"))
    assert excinfo.value.status_code == 404


async def test_a_page_an_agent_wrote_is_served_sealed(north_home) -> None:
    """An HTML artifact ran its scripts with north's rights when opened from north's own address."""
    (north_home / "tasks" / "task_abc123" / "deck.html").write_text("<script>fetch('/web/api/secrets')</script>")
    (north_home / "tasks" / "task_abc123" / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    page = await web_api.get_artifact_file(_id_for("tasks/task_abc123/deck.html"))
    picture = await web_api.get_artifact_file(_id_for("tasks/task_abc123/chart.png"))

    assert page.headers["content-security-policy"] == "sandbox allow-scripts"
    assert "content-security-policy" not in picture.headers


async def test_show_in_the_file_manager_reveals_the_file_and_never_opens_it(north_home, monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr("click.launch", lambda target, locate=False, **_: calls.append((target, locate)) or 0)

    assert await web_api.reveal_artifact(_id_for("tasks/task_abc123/research/context.md")) == {"shown": True}

    assert calls == [(str(north_home / "tasks" / "task_abc123" / "research" / "context.md"), True)]


async def test_show_in_the_file_manager_cannot_reach_outside_the_library(north_home, monkeypatch) -> None:
    monkeypatch.setattr("click.launch", lambda *_args, **_kwargs: pytest.fail("nothing outside the library is shown"))

    with pytest.raises(HTTPException) as excinfo:
        await web_api.reveal_artifact(_id_for("secret.key"))
    assert excinfo.value.status_code == 404


async def test_a_computer_with_no_file_manager_says_so(north_home, monkeypatch) -> None:
    monkeypatch.setattr("click.launch", lambda *_args, **_kwargs: 1)  # xdg-open missing, as in a container

    with pytest.raises(HTTPException) as excinfo:
        await web_api.reveal_artifact(_id_for("tasks/task_abc123/research/context.md"))
    assert excinfo.value.status_code == 409
