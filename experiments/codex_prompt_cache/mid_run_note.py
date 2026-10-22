"""Two live Codex calls: does a system note added mid-run keep the earlier prompt cached?

Call 1 sends the general agent's instructions and a task. Call 2 sends the same
plus the model's reply and the soft-budget note, as a system message, exactly as
the agent loop appends it. Whatever call 2 gets from cache is what the provider's
handling of that note left intact. About 14k input tokens per arm, from the Codex plan.
Call 2 waits --pause seconds first, as a real run waits while its tools run (5-20 s);
sent at once, it sometimes found nothing cached at all.

Arms:
    in-place  what the provider sends now: the note stays at the end, as a user note
    merged    what it sent before: the note joined onto the instructions

Run:
    .venv/bin/python -m experiments.codex_prompt_cache.mid_run_note --arm merged in-place merged in-place
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from experiments.codex_prompt_cache.probe import MODEL, general_instructions, task_message
from inference.codex_auth import CodexCredentialProvider
from inference.models import ToolCallRequest
from inference.providers.openai_codex import OpenAICodexProvider

SOFT_BUDGET_NOTE = (
    "Soft efficiency budget reached. If the requested result is supported by the evidence already "
    "collected, synthesize the final answer now."
)


def second_call_messages(first: list[dict], reply: str, arm: str) -> list[dict]:
    """Call 2's transcript. "merged" rebuilds what the old provider sent: instructions + "\\n\\n" + note."""
    if arm == "merged":
        system = {"role": "system", "content": f"{first[0]['content']}\n\n{SOFT_BUDGET_NOTE}"}
        return [system, *first[1:], {"role": "assistant", "content": reply}]
    return [*first, {"role": "assistant", "content": reply}, {"role": "system", "content": SOFT_BUDGET_NOTE}]


async def run(arm: str, pause: float) -> dict[str, int]:
    session = f"probe-{uuid.uuid4().hex}"
    messages = [
        {"role": "system", "content": general_instructions()},
        {"role": "user", "content": task_message(uuid.uuid4().hex)},
    ]
    provider = OpenAICodexProvider(CodexCredentialProvider())
    try:
        first = await provider.complete_with_tools(
            MODEL, ToolCallRequest(messages=messages, tools=[], component="mid_run_note_probe", run_id=session)
        )
        await asyncio.sleep(pause)
        second = await provider.complete_with_tools(
            MODEL,
            ToolCallRequest(
                messages=second_call_messages(messages, first.content or "", arm),
                tools=[],
                component="mid_run_note_probe",
                run_id=session,
            ),
        )
    finally:
        await provider.aclose()
    return {
        "first_tokens_in": first.tokens_in,
        "second_tokens_in": second.tokens_in,
        "second_cached": second.cached_tokens,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("in-place", "merged"), nargs="+", default=("in-place",))
    parser.add_argument("--pause", type=float, default=5.0, help="Seconds between call 1 and call 2.")
    args = parser.parse_args()
    print("arm       first_in  second_in  second_cached")
    for arm in args.arm:
        result = asyncio.run(run(arm, args.pause))
        print(
            f"{arm:<8} {result['first_tokens_in']:>9,} {result['second_tokens_in']:>10,} {result['second_cached']:>14,}"
        )


if __name__ == "__main__":
    main()
