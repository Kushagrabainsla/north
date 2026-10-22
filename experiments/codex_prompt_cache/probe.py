"""Four live Codex calls that show which part of the prompt a cache session can reuse.

Each call sends the general agent's real instructions and real tool schemas:

1. session S: tools A + task 1             warms S
2. session S: tools B + task 2             B differs from A only in its first tool
3. session S: tools A + task 2             same tools, other task
4. session T: exact copy of call 1         a new session

Calls 2 and 3 run at once, which also shows one session takes parallel requests.
About 45k input tokens in all, from the Codex plan.

Run:
    .venv/bin/python -m experiments.codex_prompt_cache.probe
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from agents.llm_agent import _DELIVERABLE_POLICY, _POLICIES, _TOOL_CREATION_POLICY
from agents.policy import render_policies
from experiments.tool_selection.retrieval_benchmark import ToolDoc, discover_tool_docs
from inference.codex_auth import CodexCredentialProvider
from inference.models import ToolCallRequest
from inference.providers.openai_codex import OpenAICodexProvider
from utils.time import RUNTIME_CONTEXT_INSTRUCTION

MODEL = "gpt-5.6-terra"
_TOOLS_PER_CALL = 12
_ROOT = Path(__file__).resolve().parents[2]


def general_instructions() -> str:
    """The general agent's system prompt, built the way LLMAgent builds it (without the persona)."""
    return (
        (_ROOT / "agents" / "general" / "prompts" / "system.md").read_text(encoding="utf-8")
        + _TOOL_CREATION_POLICY
        + _DELIVERABLE_POLICY
        + f"\n\n{RUNTIME_CONTEXT_INSTRUCTION}"
        + render_policies(_POLICIES, "general")
    )


def function_schema(doc: ToolDoc) -> dict:
    """A tool as the agent sends it to the provider."""
    function = {
        "name": doc.name,
        "description": doc.description,
        "parameters": doc.parameters_schema or {"type": "object"},
    }
    return {"type": "function", "function": function}


def tool_sets() -> tuple[list[dict], list[dict]]:
    """Two sorted tool lists that differ only in their first tool."""
    docs = discover_tool_docs()
    names = sorted(docs)
    tools_a = [function_schema(docs[name]) for name in names[1 : _TOOLS_PER_CALL + 1]]
    tools_b = [function_schema(docs[names[0]]), *tools_a[1:], function_schema(docs[names[_TOOLS_PER_CALL + 1]])]
    return tools_a, tools_b


def task_message(tag: str) -> str:
    """A task message of about 3k tokens that differs from any other from its second line."""
    line = "- note {tag}-{i}: background line about the user's projects and schedule."
    background = "\n".join(line.format(tag=tag, i=i) for i in range(120))
    task = "Do not call any tool. Reply with the single word OK."
    return f"## Task ID\n{tag}\n\n## Context\n{background}\n\n## Task\n{task}"


async def _call(
    provider: OpenAICodexProvider, instructions: str, session: str, tools: list[dict], task: str
) -> dict[str, int]:
    response = await provider.complete_with_tools(
        MODEL,
        ToolCallRequest(
            messages=[{"role": "system", "content": instructions}, {"role": "user", "content": task}],
            tools=tools,
            component="codex_prompt_cache_probe",
            run_id=session,  # sent as the session_id header
        ),
    )
    return {"tokens_in": response.tokens_in, "cached_tokens": response.cached_tokens}


async def run() -> list[dict]:
    instructions = general_instructions()
    tools_a, tools_b = tool_sets()
    session, other_session = f"probe-{uuid.uuid4().hex}", f"probe-{uuid.uuid4().hex}"
    task_1, task_2 = task_message(uuid.uuid4().hex), task_message(uuid.uuid4().hex)
    provider = OpenAICodexProvider(CodexCredentialProvider())
    try:
        first = await _call(provider, instructions, session, tools_a, task_1)
        second, third = await asyncio.gather(
            _call(provider, instructions, session, tools_b, task_2),
            _call(provider, instructions, session, tools_a, task_2),
        )
        fourth = await _call(provider, instructions, other_session, tools_a, task_1)
    finally:
        await provider.aclose()
    return [
        {"call": 1, "session": "S", "prompt": "tools A + task 1", **first},
        {"call": 2, "session": "S", "prompt": "tools B (first tool differs) + task 2", **second},
        {"call": 3, "session": "S", "prompt": "tools A + task 2", **third},
        {"call": 4, "session": "T (new)", "prompt": "copy of call 1", **fourth},
    ]


def main() -> None:
    print("call  session  tokens_in  cached  prompt")
    for row in asyncio.run(run()):
        counts = f"{row['tokens_in']:>9}  {row['cached_tokens']:>6}"
        print(f"{row['call']:>4}  {row['session']:<7}  {counts}  {row['prompt']}")


if __name__ == "__main__":
    main()
