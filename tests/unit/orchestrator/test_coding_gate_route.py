"""The loopback route a coding agent's hook asks: its own token, never the API secret."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from coding_agents import Decision, Gate, GateSessions, Verdict
from orchestrator.api import coding_gate_router
from orchestrator.api_context import ApiServices, attach


class Judge:
    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        self.asked: list = []

    async def judge(self, session, request, *, inside_worktree):
        self.asked.append((session.task_id, request.command, inside_worktree))
        return self.verdict


def _payload(command: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}


@pytest.fixture
def judge() -> Judge:
    return Judge(Verdict(Decision.ALLOW, "you said yes"))


@pytest.fixture
def sessions() -> GateSessions:
    return GateSessions()


@pytest.fixture
def client(sessions, judge):
    app = FastAPI()
    attach(app, ApiServices(coding_sessions=sessions, coding_gate=Gate(judge)))
    app.include_router(coding_gate_router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://daemon")


async def _post(client, token: str | None, **kwargs):
    headers = {"X-Gate-Token": token} if token is not None else {}
    return await client.post("/orchestrator/coding/gate", headers=headers, **kwargs)


async def test_a_run_with_its_token_gets_a_decision_in_the_shape_claude_code_reads(client, sessions, judge) -> None:
    session = sessions.issue("run-1", "t1", "/wt")

    response = await _post(client, session.token, json=_payload("make test"))

    assert response.status_code == 200
    assert response.json() == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "you said yes",
        }
    }
    assert judge.asked == [("t1", "make test", False)]


async def test_a_denial_is_carried_back_with_its_reason(client, sessions, judge) -> None:
    judge.verdict = Verdict(Decision.DENY, "The user declined this action.")
    session = sessions.issue("run-1", "t1", "/wt")

    answer = (await _post(client, session.token, json=_payload("rm -rf ."))).json()["hookSpecificOutput"]

    assert (answer["permissionDecision"], answer["permissionDecisionReason"]) == (
        "deny",
        "The user declined this action.",
    )


async def test_a_plainly_read_only_command_is_passed_so_the_vendor_decides(client, sessions, judge) -> None:
    session = sessions.issue("run-1", "t1", "/wt")

    response = await _post(client, session.token, json=_payload("git status"))

    assert response.json() == {"pass": True} and judge.asked == []


@pytest.mark.parametrize("token", [None, "", "guess", "x" * 43])
async def test_no_token_or_a_token_no_run_holds_is_refused(client, token) -> None:
    response = await _post(client, token, json=_payload("make test"))

    assert response.status_code == 403


async def test_a_token_stops_working_when_the_run_ends(client, sessions) -> None:
    session = sessions.issue("run-1", "t1", "/wt")
    sessions.revoke(session.token)

    assert (await _post(client, session.token, json=_payload("make test"))).status_code == 403


async def test_a_body_that_is_not_json_is_refused_so_the_hook_fails_closed(client, sessions) -> None:
    session = sessions.issue("run-1", "t1", "/wt")

    response = await _post(client, session.token, content=b"not json")

    assert response.status_code == 400


async def test_a_payload_it_cannot_read_is_denied_not_passed(client, sessions) -> None:
    session = sessions.issue("run-1", "t1", "/wt")

    answer = (await _post(client, session.token, json={"nonsense": True})).json()["hookSpecificOutput"]

    assert answer["permissionDecision"] == "deny"
