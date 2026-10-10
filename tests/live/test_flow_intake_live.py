"""Small real-model flow-intake probes; proposed tools are never executed.

NORTH_LIVE_FLOW_INTAKE=1 .venv/bin/pytest tests/live/test_flow_intake_live.py -q -s
Uses an already-fresh North token read-only: no refresh, credential copies,
daemon changes, authoring, activation, or schedule installation.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from agents.schemas import ASK_USER_SCHEMA
from inference.auth import AuthContext
from inference.codex_auth import CodexTokenStore
from inference.models import ToolCallRequest
from inference.providers.openai_codex import OpenAICodexProvider
from skills.registry import SkillRegistry
from tools.universal.create_flow import CreateFlowTool
from utils.runtime_resources import builtin_skills_dir

pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_FLOW_INTAKE") != "1", reason="set NORTH_LIVE_FLOW_INTAKE=1 to use the real model"
)


@pytest.fixture
async def flow_model():
    token = CodexTokenStore(Path.home() / ".north" / "credentials" / "openai_codex.json").load()
    if token is None or not token.is_fresh:
        pytest.skip("probe requires a fresh North login; it never refreshes credentials")

    class ReadOnlyCredentials:
        async def get_auth(self):
            headers = {"Authorization": f"Bearer {token.access_token}"}
            if token.account_id:
                headers["ChatGPT-Account-Id"] = token.account_id
            return AuthContext(headers=headers, source="read_only_probe")

    provider = OpenAICodexProvider(ReadOnlyCredentials())
    try:
        yield provider
    finally:
        await provider.aclose()


async def _intake(provider, timing):
    skill = SkillRegistry(builtin_dir=builtin_skills_dir()).get("authoring-a-north-flow")
    system = (Path(__file__).parents[2] / "agents/general/prompts/system.md").read_text()
    system += "\n\n## Selected procedure\n" + skill.body
    system += (
        "\n\n<north_runtime_context>\ntimezone: America/Los_Angeles\n</north_runtime_context>\n"
        "## Context\nNo user timing preference is stored.\n"
        "## Verified capability catalog\nNo flows or schedules exist. Active skill review-saved-note: "
        "general executor, read_file only, approval never, required string input note_path, "
        "output summary string. It returns a concise summary of one saved note without writing or sending."
    )
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "Create a reusable custom flow called note-review. Use review-saved-note on "
                "/tmp/intake-fixture/note.md and return the summary in chat. One note per run; "
                "no writing, sending, browser, or submission. Leave it a candidate; do not test or activate yet. "
                + timing
            ),
        },
    ]
    tools = [
        ASK_USER_SCHEMA,
        {
            "type": "function",
            "function": {
                "name": CreateFlowTool.name,
                "description": CreateFlowTool.description,
                "parameters": CreateFlowTool.parameters_schema,
            },
        },
    ]
    # Discovery is allowed before intake. Supply synthetic read-only results;
    # never execute the proposed mutations, even if the model jumps ahead.
    async with asyncio.timeout(90):
        for _ in range(4):
            response = await provider.complete_with_tools(
                os.environ.get("NORTH_FLOW_INTAKE_MODEL", "gpt-5.6-terra"),
                ToolCallRequest(messages=messages, tools=tools, component="general"),
            )
            interesting = [
                call
                for call in response.calls
                if call.name == "ask_user" or call.params.get("action") not in {"list", "read", "validate"}
            ]
            if interesting or not response.calls:
                return response
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": json.dumps(call.params)},
                        }
                        for call in response.calls
                    ],
                }
            )
            for call in response.calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps({"success": True, "data": {"flows": []}}),
                    }
                )
    pytest.fail("intake did not reach a clarification or authoring decision")


@pytest.mark.parametrize("repeat", range(2))
@pytest.mark.parametrize(
    ("timing", "needs_question"),
    [
        pytest.param("", True, id="missing-timing"),
        pytest.param("Make it recurring.", True, id="incomplete-recurring"),
        pytest.param("Manual only: run it when I ask, with no schedule.", False, id="explicit-manual"),
        pytest.param(
            "Eventually run it weekdays at 09:30; no schedule installation yet.",
            False,
            id="explicit-recurring",
        ),
    ],
)
async def test_flow_creation_resolves_timing_before_authoring(flow_model, timing, needs_question, repeat):
    del repeat
    response = await _intake(flow_model, timing)
    calls = [(call.name, call.params) for call in response.calls]
    print("Intake:", timing or "<unspecified>", calls, response.content or "")
    for call in response.calls:
        if call.name == "ask_user":
            question = call.params["question"].lower()
            assert not any(phrase in question for phrase in ("which timezone", "what timezone", "your timezone?"))
        if call.name == "create_flow":
            for step in call.params.get("steps", []):
                assert not {"timezone", "tz"} & step.get("inputs", {}).keys()
    if needs_question:
        assert response.calls and all(call.name == "ask_user" for call in response.calls), calls
        questions = " ".join(json.dumps(call.params) for call in response.calls).lower()
        assert any(word in questions for word in ("when", "schedule", "time", "manual", "often")), questions
        if not timing:
            assert "manual" in questions and any(word in questions for word in ("recurr", "repeat")), questions
    else:
        assert not any(call.name == "ask_user" for call in response.calls), calls
        assert any(call.name == "create_flow" and call.params.get("action") == "create" for call in response.calls)
