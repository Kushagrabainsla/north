from __future__ import annotations

from web.conversations import ConversationStore


async def test_conversation_lifecycle_and_turn_order(tmp_path) -> None:
    store = ConversationStore(tmp_path / "web.db")

    conversation = await store.create()
    assert conversation.title == "New chat"

    first = await store.add_turn(conversation.id, "Design the cockpit")
    second = await store.add_turn(conversation.id, "Now build the chat room")
    await store.attach_task(first.id, "task_1")

    turns = await store.turns(conversation.id)
    assert [turn.position for turn in turns] == [1, 2]
    assert turns[0].task_id == "task_1"
    assert turns[1].prompt == second.prompt

    renamed = await store.update(conversation.id, title="North web", pinned=True)
    assert renamed is not None
    assert renamed.title == "North web"
    assert renamed.pinned is True


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
