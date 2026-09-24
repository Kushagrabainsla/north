from __future__ import annotations

import sqlite3

from orchestrator.api_context import ApiServices, bind_services
from orchestrator.models import TaskResponse
from web import api as web_api
from web.conversations import ConversationStore


def test_legacy_waiting_answer_is_not_labeled_complete() -> None:
    assert (
        web_api._legacy_outcome_status("Please confirm Chrome is open. Once it does, I'll continue.")
        == "waiting_for_user"
    )
    assert web_api._legacy_outcome_status("Do you want me to continue?") == "waiting_for_user"
    assert (
        web_api._legacy_outcome_status("Once it is open, tell me and I will verify the connection.")
        == "waiting_for_user"
    )
    assert web_api._legacy_outcome_status("Can we do that?") == "waiting_for_user"
    assert web_api._legacy_outcome_status("The requested summary is ready.") == "response_ready"


async def test_conversation_lifecycle_and_turn_order(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")

    conversation = await store.create(workspace=str(tmp_path))
    assert conversation.title == "New chat"
    assert conversation.workspace == str(tmp_path)
    assert conversation.source == "web"
    assert conversation.goal == ""
    assert conversation.goal_status == "idle"

    first = await store.add_turn(conversation.id, "Design the cockpit")
    second = await store.add_turn(conversation.id, "Now build the chat room")
    await store.attach_task(first.id, "task_1")

    turns = await store.turns(conversation.id)
    assert [turn.position for turn in turns] == [1, 2]
    assert turns[0].task_id == "task_1"
    assert turns[1].prompt == second.prompt
    active = await store.get(conversation.id)
    assert active is not None
    assert active.goal == "Design the cockpit"
    assert active.goal_status == "active"
    listed = await store.list()
    assert listed[0].turn_count == 2

    other_workspace = tmp_path / "other"
    renamed = await store.update(conversation.id, title="North web", workspace=str(other_workspace), pinned=True)
    assert renamed is not None
    assert renamed.title == "North web"
    assert renamed.pinned is True
    assert renamed.workspace == str(other_workspace)


async def test_existing_database_is_migrated_with_workspace_and_source(tmp_path) -> None:
    db_path = tmp_path / "web.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE web_conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )

    store = ConversationStore(db_path)
    conversation = await store.create()
    assert conversation.workspace == ""
    assert conversation.source == "web"
    assert conversation.goal == ""
    assert conversation.goal_status == "idle"


async def test_existing_session_is_backfilled_from_its_first_prompt(tmp_path) -> None:
    db_path = tmp_path / "web.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE web_conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE web_turns (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, position INTEGER NOT NULL,
                prompt TEXT NOT NULL, task_id TEXT, created_at TEXT NOT NULL
            );
            INSERT INTO web_conversations VALUES ('session-1', 'New session', 0, 0, 'now', 'now');
            INSERT INTO web_turns VALUES ('turn-2', 'session-1', 2, 'A follow-up', NULL, 'now');
            INSERT INTO web_turns VALUES ('turn-1', 'session-1', 1, 'Build the original outcome', NULL, 'now');
            """
        )

    store = ConversationStore(db_path)
    conversation = await store.get("session-1")

    assert conversation is not None
    assert conversation.title == "Build the original outcome"
    assert conversation.goal == "Build the original outcome"
    assert conversation.goal_status == "active"


async def test_chat_turn_uses_its_conversation_workspace(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    conversation = await store.create(workspace=str(workspace))

    class Orchestrator:
        request = None

        async def submit_task(self, request):
            self.request = request
            return TaskResponse(task_id="task_1", status="pending", created_at="2026-01-01T00:00:00Z")

    orchestrator = Orchestrator()
    with bind_services(ApiServices(conversation_store=store, orchestrator=orchestrator)):
        await web_api.create_turn(conversation.id, web_api.TurnCreate(prompt="Inspect this project"))

    assert orchestrator.request is not None
    assert orchestrator.request.workspace == str(workspace)
    assert "## Active session goal" in orchestrator.request.context
    assert "Goal: Inspect this project" in orchestrator.request.context


async def test_new_chat_inherits_the_server_workspace(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")

    class Settings:
        north_workspace = str(tmp_path)

    with bind_services(ApiServices(conversation_store=store, north_settings=Settings())):
        payload = await web_api.create_conversation(web_api.ConversationCreate())

    assert payload["workspace"] == str(tmp_path.resolve())


async def test_cli_conversation_is_visible_in_shared_list(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")

    class Settings:
        north_workspace = str(tmp_path)

    with bind_services(ApiServices(conversation_store=store, north_settings=Settings())):
        payload = await web_api.create_conversation(web_api.ConversationCreate(source="cli"))
        listed = await web_api.list_conversations()

    assert payload["source"] == "cli"
    assert [(item["id"], item["source"]) for item in listed] == [(payload["id"], "cli")]


async def test_workspace_picker_lists_real_directories(tmp_path) -> None:
    (tmp_path / "Project Alpha").mkdir()
    (tmp_path / "project-beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("not a directory")

    listing = web_api._workspace_listing(str(tmp_path))

    assert listing["path"] == str(tmp_path.resolve())
    assert listing["parent"] == str(tmp_path.parent.resolve())
    assert listing["directories"] == [
        {"name": "Project Alpha", "path": str((tmp_path / "Project Alpha").resolve())},
        {"name": "project-beta", "path": str((tmp_path / "project-beta").resolve())},
    ]


async def test_first_prompt_titles_new_conversation_and_searches_safely(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")
    conversation = await store.create()
    await store.add_turn(conversation.id, "A detailed dashboard with every subsystem")

    updated = await store.get(conversation.id)
    assert updated is not None
    assert updated.title == "A detailed dashboard with every subsystem"
    assert [item.id for item in await store.list(query="dashboard")] == [conversation.id]
    assert await store.list(query="%") == []


async def test_first_prompt_titles_new_session_and_sets_durable_goal(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")
    conversation = await store.create("New session")

    await store.add_turn(conversation.id, "Build a safe job application flow")

    updated = await store.get(conversation.id)
    assert updated is not None
    assert updated.title == "Build a safe job application flow"
    assert updated.goal == "Build a safe job application flow"
    assert updated.goal_status == "active"


async def test_user_reply_resumes_waiting_goal_without_replacing_it(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")
    conversation = await store.create()
    await store.add_turn(conversation.id, "Build a safe job application flow")
    await store.update(conversation.id, goal_status="waiting_for_user")

    await store.add_turn(conversation.id, "Use my existing browser")

    updated = await store.get(conversation.id)
    assert updated is not None
    assert updated.goal == "Build a safe job application flow"
    assert updated.goal_status == "active"


async def test_derived_goal_status_does_not_change_session_activity_time(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")
    conversation = await store.create()
    await store.add_turn(conversation.id, "Original goal")
    before = await store.get(conversation.id)

    updated = await store.update(
        conversation.id,
        goal_status="waiting_for_user",
        touch_updated_at=False,
    )

    assert before is not None
    assert updated is not None
    assert updated.goal_status == "waiting_for_user"
    assert updated.updated_at == before.updated_at


async def test_archived_conversations_leave_active_list(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")
    conversation = await store.create("Archive me")
    await store.update(conversation.id, archived=True)

    assert await store.list() == []
    archived = await store.list(archived=True)
    assert [item.id for item in archived] == [conversation.id]
