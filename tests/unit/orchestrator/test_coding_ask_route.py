"""The loopback MCP route `ask_north` is served on: its own token, never the API secret."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from coding_agents import GateSessions
from coding_agents.ask import Question, Reply
from orchestrator.api import coding_ask_router
from orchestrator.api_context import ApiServices, attach


class Asker:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str, Question]] = []

    async def ask(self, session, question: Question) -> Reply:
        self.asked.append((session.run_id, session.task_id, question))
        return Reply("spaces", by="The user")


@pytest.fixture
def asker() -> Asker:
    return Asker()


@pytest.fixture
def sessions() -> GateSessions:
    return GateSessions()


@pytest.fixture
def client(sessions, asker):
    app = FastAPI()
    attach(app, ApiServices(coding_sessions=sessions, coding_asker=asker))
    app.include_router(coding_ask_router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://daemon")


def _call(question: str = "Tabs or spaces?") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "ask_north", "arguments": {"question": question}},
    }


async def _post(client, token: str | None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return await client.post("/orchestrator/coding/ask", headers=headers, **kwargs)


async def test_a_run_with_its_token_can_ask_and_gets_the_answer_as_an_mcp_tool_result(client, sessions, asker) -> None:
    session = sessions.issue("run-1", "t1", "/wt", editing=False)

    response = await _post(client, session.token, json=_call())

    assert response.status_code == 200
    assert response.json()["result"]["content"][0]["text"] == "The user answered: spaces"
    assert asker.asked == [("run-1", "t1", Question("Tabs or spaces?"))], "the run is the token's, never the body's"


async def test_a_handshake_and_the_tool_list_work_over_the_same_route(client, sessions) -> None:
    session = sessions.issue("run-1", "t1", "/wt")

    initialized = await _post(
        client, session.token, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
    )
    listed = await _post(client, session.token, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert initialized.json()["result"]["serverInfo"]["name"] == "north"
    assert [t["name"] for t in listed.json()["result"]["tools"]] == ["ask_north"]


async def test_a_notification_is_accepted_with_nothing_to_say(client, sessions) -> None:
    session = sessions.issue("run-1", "t1", "/wt")

    response = await _post(client, session.token, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert response.status_code == 202 and response.content == b""


@pytest.mark.parametrize("token", [None, "", "guess"])
async def test_no_token_or_a_token_no_run_holds_is_refused_and_nothing_is_asked(client, asker, token) -> None:
    response = await _post(client, token, json=_call())

    assert response.status_code == 403 and asker.asked == []


async def test_the_token_stops_working_when_its_run_ends(client, sessions, asker) -> None:
    session = sessions.issue("run-1", "t1", "/wt")
    sessions.revoke(session.token)

    response = await _post(client, session.token, json=_call())

    assert response.status_code == 403 and asker.asked == []


async def test_a_body_that_is_not_json_is_a_json_rpc_parse_error(client, sessions) -> None:
    session = sessions.issue("run-1", "t1", "/wt")

    response = await _post(client, session.token, content=b"not json")

    assert response.status_code == 400 and response.json()["error"]["code"] == -32700


async def test_the_route_is_post_only(client, sessions) -> None:
    session = sessions.issue("run-1", "t1", "/wt")

    response = await client.get("/orchestrator/coding/ask", headers={"Authorization": f"Bearer {session.token}"})

    assert response.status_code == 405
