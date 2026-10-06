"""`ask_north`: the MCP door a coding agent has back to north. Pure protocol; who answers is not decided here."""

from __future__ import annotations

import pytest

from coding_agents.ask import (
    CLAUDE_FETCH_TOOL,
    CLAUDE_TOOL,
    FETCH_TOOL,
    MAX_OPTIONS,
    MAX_QUESTION_CHARS,
    TOOL,
    Question,
    Reply,
    handle,
    render,
)
from coding_agents.gate import CLAUDE_ASK_TOOL, GateSession
from coding_agents.gate import CLAUDE_FETCH_TOOL as GATE_FETCH_TOOL

SESSION = GateSession("tok", "run-1", "t1", "/wt")


class FakeFetcher:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def fetch(self, session: GateSession, url: str) -> Reply:
        self.urls.append(url)
        return Reply("page text")


class FakeAsker:
    def __init__(self, reply: Reply | None = None) -> None:
        self.reply = reply or Reply("spaces", by="The user")
        self.asked: list[Question] = []

    async def ask(self, session: GateSession, question: Question) -> Reply:
        self.asked.append(question)
        return self.reply


def _call(arguments, name: str = TOOL, ident: int = 7) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_the_gate_and_the_protocol_spell_the_one_allowed_tool_the_same_way() -> None:
    assert CLAUDE_TOOL == CLAUDE_ASK_TOOL == "mcp__north__ask_north"
    assert CLAUDE_FETCH_TOOL == GATE_FETCH_TOOL == "mcp__north__fetch_url"


class TestHandshake:
    async def test_initialize_answers_with_the_clients_protocol_version_when_it_is_supported(self) -> None:
        reply = await handle(
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            SESSION,
            FakeAsker(),
        )

        assert reply["result"]["protocolVersion"] == "2025-06-18"
        assert reply["result"]["capabilities"] == {"tools": {}} and reply["result"]["serverInfo"]["name"] == "north"

    async def test_an_unknown_protocol_version_gets_the_newest_one_north_speaks(self) -> None:
        reply = await handle(
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            SESSION,
            FakeAsker(),
        )

        assert reply["result"]["protocolVersion"] == "2025-11-25"

    async def test_a_notification_gets_no_reply(self) -> None:
        assert await handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, SESSION, FakeAsker()) is None

    async def test_ping_is_answered(self) -> None:
        assert (await handle({"jsonrpc": "2.0", "id": 3, "method": "ping"}, SESSION, FakeAsker()))["result"] == {}

    async def test_the_tool_list_has_exactly_one_tool_and_it_asks_for_a_question(self) -> None:
        reply = await handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, SESSION, FakeAsker())

        [tool] = reply["result"]["tools"]
        assert tool["name"] == TOOL and tool["inputSchema"]["required"] == ["question"]
        assert "never ask for passwords" in tool["description"].lower()

    async def test_the_tool_says_it_changes_nothing_so_a_vendors_plan_mode_lets_it_through(self) -> None:
        reply = await handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, SESSION, FakeAsker())

        [tool] = reply["result"]["tools"]
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["destructiveHint"] is False

    async def test_any_other_method_is_refused_by_name(self) -> None:
        reply = await handle({"jsonrpc": "2.0", "id": 9, "method": "resources/read"}, SESSION, FakeAsker())

        assert reply["error"]["code"] == -32601 and "resources/read" in reply["error"]["message"]

    async def test_a_batch_is_answered_message_by_message_and_notifications_are_left_out(self) -> None:
        replies = await handle(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ],
            SESSION,
            FakeAsker(),
        )

        assert [r["id"] for r in replies] == [1, 2]

    async def test_something_that_is_not_a_message_is_an_error(self) -> None:
        assert (await handle("hello", SESSION, FakeAsker()))["error"]["code"] == -32600


class TestAskingAQuestion:
    async def test_the_question_reaches_the_asker_and_the_answer_comes_back_labelled(self) -> None:
        asker = FakeAsker(Reply("spaces", by="The user"))

        reply = await handle(_call({"question": "Tabs or spaces?", "options": ["tabs", "spaces"]}), SESSION, asker)

        assert asker.asked == [Question("Tabs or spaces?", ("tabs", "spaces"))]
        assert reply["id"] == 7
        assert reply["result"] == {"content": [{"type": "text", "text": "The user answered: spaces"}], "isError": False}

    async def test_an_answer_from_memory_says_why(self) -> None:
        asker = FakeAsker(Reply("spaces", by="North, answering for the user", reason="you use spaces everywhere"))

        reply = await handle(_call({"question": "Tabs or spaces?"}), SESSION, asker)

        text = reply["result"]["content"][0]["text"]
        assert text == "North, answering for the user answered: spaces (why: you use spaces everywhere)"

    async def test_no_answer_tells_the_agent_what_to_do_instead(self) -> None:
        asker = FakeAsker(Reply("Choose the safest option and say so.", answered=False))

        reply = await handle(_call({"question": "Which db?"}), SESSION, asker)

        assert reply["result"]["content"][0]["text"] == "Choose the safest option and say so."
        assert reply["result"]["isError"] is False, "an unanswered question is not a broken tool"

    @pytest.mark.parametrize(
        ("arguments", "complaint"),
        [
            (None, "needs a question"),
            ({}, "non-empty question"),
            ({"question": "   "}, "non-empty question"),
            ({"question": "x" * (MAX_QUESTION_CHARS + 1)}, "too long"),
            ({"question": "ok", "options": "tabs"}, "list of strings"),
        ],
    )
    async def test_a_malformed_call_is_refused_without_bothering_anyone(self, arguments, complaint) -> None:
        asker = FakeAsker()

        reply = await handle(_call(arguments), SESSION, asker)

        assert complaint in reply["result"]["content"][0]["text"] and reply["result"]["isError"] is True
        assert asker.asked == []

    async def test_options_are_trimmed_and_capped(self) -> None:
        asker = FakeAsker()
        many = [f" option {n} " for n in range(MAX_OPTIONS + 5)] + ["", "  "]

        await handle(_call({"question": "Which?", "options": many}), SESSION, asker)

        [question] = asker.asked
        assert len(question.options) == MAX_OPTIONS and question.options[0] == "option 0"

    async def test_calling_a_tool_that_does_not_exist_is_an_error_and_asks_nothing(self) -> None:
        asker = FakeAsker()

        reply = await handle(_call({"question": "x"}, name="read_memory"), SESSION, asker)

        assert reply["error"]["code"] == -32602 and asker.asked == []


def test_render_names_who_answered_only_when_it_knows() -> None:
    assert render(Reply("spaces")) == "Answer: spaces"
    assert render(Reply("spaces", by="The user")) == "The user answered: spaces"


class TestFetchingAPage:
    async def test_without_a_fetcher_the_tool_is_not_offered_and_not_callable(self) -> None:
        listed = await handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, SESSION, FakeAsker())
        called = await handle(_call({"url": "https://x.io"}, name=FETCH_TOOL), SESSION, FakeAsker())

        assert [t["name"] for t in listed["result"]["tools"]] == [TOOL]
        assert called["error"]["code"] == -32602

    async def test_with_a_fetcher_both_tools_are_offered_and_the_fetch_one_says_it_reaches_out(self) -> None:
        reply = await handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, SESSION, FakeAsker(), FakeFetcher())

        tools = {t["name"]: t for t in reply["result"]["tools"]}
        assert set(tools) == {TOOL, FETCH_TOOL}
        assert tools[FETCH_TOOL]["annotations"]["readOnlyHint"] is True
        assert tools[FETCH_TOOL]["annotations"]["openWorldHint"] is True

    async def test_a_fetch_reaches_the_fetcher_not_the_asker(self) -> None:
        asker, fetcher = FakeAsker(), FakeFetcher()

        reply = await handle(_call({"url": " https://x.io/a "}, name=FETCH_TOOL), SESSION, asker, fetcher)

        assert fetcher.urls == ["https://x.io/a"] and asker.asked == []
        assert reply["result"] == {"content": [{"type": "text", "text": "page text"}], "isError": False}

    @pytest.mark.parametrize("arguments", [None, {}, {"url": ""}, {"url": "file:///etc/passwd"}, {"url": "ftp://x.io"}])
    async def test_a_url_that_is_not_http_is_refused_before_anyone_is_asked(self, arguments) -> None:
        fetcher = FakeFetcher()

        reply = await handle(_call(arguments, name=FETCH_TOOL), SESSION, FakeAsker(), fetcher)

        assert reply["result"]["isError"] is True and fetcher.urls == []
