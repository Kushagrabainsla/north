from __future__ import annotations

import sqlite3

from orchestrator.api_context import ApiServices, bind_services
from orchestrator.models import TaskResponse
from web import api as web_api
from web.conversations import ConversationStore


async def test_conversation_lifecycle_and_turn_order(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")

    conversation = await store.create(workspace=str(tmp_path))
    assert conversation.title == "New chat"
    assert conversation.workspace == str(tmp_path)

    first = await store.add_turn(conversation.id, "Design the cockpit")
    second = await store.add_turn(conversation.id, "Now build the chat room")
    await store.attach_task(first.id, "task_1")

    turns = await store.turns(conversation.id)
    assert [turn.position for turn in turns] == [1, 2]
    assert turns[0].task_id == "task_1"
    assert turns[1].prompt == second.prompt

    other_workspace = tmp_path / "other"
    renamed = await store.update(conversation.id, title="North web", workspace=str(other_workspace), pinned=True)
    assert renamed is not None
    assert renamed.title == "North web"
    assert renamed.pinned is True
    assert renamed.workspace == str(other_workspace)


async def test_existing_database_is_migrated_with_workspace(tmp_path) -> None:
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


async def test_new_chat_inherits_the_server_workspace(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")

    class Settings:
        north_workspace = str(tmp_path)

    with bind_services(ApiServices(conversation_store=store, north_settings=Settings())):
        payload = await web_api.create_conversation(web_api.ConversationCreate())

    assert payload["workspace"] == str(tmp_path.resolve())


async def test_first_prompt_titles_new_conversation_and_searches_safely(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")
    conversation = await store.create()
    await store.add_turn(conversation.id, "A detailed dashboard with every subsystem")

    updated = await store.get(conversation.id)
    assert updated is not None
    assert updated.title == "A detailed dashboard with every subsystem"
    assert [item.id for item in await store.list(query="dashboard")] == [conversation.id]
    assert await store.list(query="%") == []


async def test_archived_conversations_leave_active_list(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")
    conversation = await store.create("Archive me")
    await store.update(conversation.id, archived=True)

    assert await store.list() == []
    archived = await store.list(archived=True)
    assert [item.id for item in archived] == [conversation.id]
