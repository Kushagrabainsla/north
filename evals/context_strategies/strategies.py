"""The strategies under comparison. One function each, same signature.

Two of them are what north does today (`truncate`, `compact`); two are what
Zhang et al. propose (`repl`, `repl_recursive`); `raw` is the control that shows
what the model does with no scaffold at all.

Adding a sixth is a function and a table entry - nothing else in the harness
knows what a strategy is.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from evals.context_strategies.corpus import Task
from evals.context_strategies.llm import Ask
from evals.context_strategies.repl import ReplSession, extract_code, extract_final

_PROMPT_DIR = Path(__file__).parent / "prompts"

# What north keeps of an oversized tool output today (agents/context_compaction.py
# uses 300 chars per output); as a whole-context budget the analogue is a head and
# tail that comfortably fit a small model's window.
_TRUNCATE_BUDGET_CHARS = 24_000
_COMPACT_CHUNK_CHARS = 20_000
# A 1M-char corpus at 20K a chunk is 50 summarising calls before the question is
# even asked. north's own compaction is bounded by the transcript, not by input
# size, so the chunk grows to keep the number of passes comparable across sizes.
_COMPACT_MAX_CHUNKS = 12
_REPL_MAX_ITERATIONS = 6
_REPL_STDOUT_KEEP = 2_000
_CONTEXT_PREVIEW_CHARS = 400


@dataclass
class StrategyRun:
    """What one strategy produced, and how it got there."""

    answer: str
    iterations: int = 1
    error: str = ""
    notes: list[str] = field(default_factory=list)


Strategy = Callable[[Task, Ask], Awaitable[StrategyRun]]


def _load(name: str) -> str:
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


async def raw(task: Task, ask: Ask) -> StrategyRun:
    """Everything in one prompt. The control, and what fails first."""
    prompt = f"{task.question}\n\n=== Records ===\n{task.context}"
    return StrategyRun(answer=await ask(prompt))


async def truncate(task: Task, ask: Ask) -> StrategyRun:
    """Cut the context to fit, keeping the head and tail. What north does now."""
    context = task.context
    if len(context) > _TRUNCATE_BUDGET_CHARS:
        half = _TRUNCATE_BUDGET_CHARS // 2
        context = f"{context[:half]}\n... [truncated to save context] ...\n{context[-half:]}"
    prompt = f"{task.question}\n\n=== Records (may be truncated) ===\n{context}"
    return StrategyRun(answer=await ask(prompt), notes=[f"kept {len(context)} of {task.context_chars} chars"])


async def compact(task: Task, ask: Ask) -> StrategyRun:
    """Summarise each chunk with the question in view, then answer from summaries.

    north's compaction agent, and the baseline Zhang et al. beat by a median of
    26%: it decides what to forget before knowing what the answer needs.
    """
    chunk_chars = max(_COMPACT_CHUNK_CHARS, -(-len(task.context) // _COMPACT_MAX_CHUNKS))
    chunks = [task.context[i : i + chunk_chars] for i in range(0, len(task.context), chunk_chars)]
    summaries = []
    for index, chunk in enumerate(chunks):
        summaries.append(
            await ask(
                f"Question that will be asked later: {task.question}\n\n"
                f"Summarise everything in this section that could bear on it. "
                f"Keep exact names, numbers and codes verbatim.\n\n"
                f"=== Section {index + 1} of {len(chunks)} ===\n{chunk}"
            )
        )
    joined = "\n\n".join(f"[Section {i + 1}] {s}" for i, s in enumerate(summaries))
    answer = await ask(f"{task.question}\n\n=== Summarised records ===\n{joined}")
    return StrategyRun(
        answer=answer,
        iterations=len(chunks) + 1,
        notes=[f"{len(chunks)} chunks of {chunk_chars} chars"],
    )


def _repl_opening(task: Task, system_prompt: str) -> str:
    return (
        f"{system_prompt}\n\n"
        f"=== Question ===\n{task.question}\n\n"
        f"=== What is in the REPL ===\n"
        f"`context` is a string of {task.context_chars} characters "
        f"({task.context.count(chr(10)) + 1} lines).\n"
        f"It begins:\n{task.context[:_CONTEXT_PREVIEW_CHARS]}\n"
    )


async def _run_repl(task: Task, ask: Ask, *, system_prompt: str, recursive: bool) -> StrategyRun:
    """The RLM loop: the model writes code, sees what it printed, and repeats."""

    async def llm_query(prompt: str) -> str:
        return await ask(prompt)

    session = ReplSession(context=task.context, llm_query=llm_query if recursive else None)
    history = _repl_opening(task, system_prompt)
    notes: list[str] = []
    # Whether any model-written code has actually run. An answer given before
    # this is a guess about a context the model has seen 400 characters of, and
    # accepting it measured how readily a model gives up rather than what the
    # strategy can do: half the REPL cells in the first real run returned
    # "UNKNOWN", "code not computed" or {"pairs": [["???", "???"]]} on turn one.
    # Scored by whether they iterated, those cells averaged 0.10 and the rest 1.00.
    executed = 0

    for iteration in range(1, _REPL_MAX_ITERATIONS + 1):
        reply = await ask(history)
        final = extract_final(reply, session)
        if final is not None and executed:
            return StrategyRun(answer=final, iterations=iteration, notes=notes)
        if final is not None:
            notes.append(f"turn {iteration}: answered before running any code")
            history += (
                f"\n\n[your reply]\n{reply}\n\n"
                "[system] You have not run any code yet, so you have not read the context - "
                "only the short preview above. Inspect `context` with a ```repl block first, "
                "then give FINAL(...)."
            )
            continue

        code = extract_code(reply)
        if not code:
            notes.append(f"turn {iteration}: no code and no FINAL")
            history += f"\n\n[your reply]\n{reply}\n\n[system] Reply with one ```repl block, or FINAL(...)."
            continue

        result = await session.run(code)
        # Counted even when it raised: the model saw the error and can correct it,
        # and requiring a *successful* run would loop a model that cannot write
        # valid code until it ran out of turns - which is a different measurement.
        executed += 1
        if result.error:
            notes.append(f"turn {iteration}: {result.error}")
        printed = result.stdout[:_REPL_STDOUT_KEEP]
        if len(result.stdout) > _REPL_STDOUT_KEEP:
            printed += f"\n... [{len(result.stdout) - _REPL_STDOUT_KEEP} more chars not shown]"
        history += (
            f"\n\n[your code]\n```repl\n{code}\n```\n"
            f"[stdout]\n{printed or '(nothing printed)'}\n"
            f"{f'[error] {result.error}' if result.error else ''}"
        )

    # Out of turns: ask once for the best answer it can give from what it found.
    answer = await ask(history + "\n\n[system] Out of turns. Reply now with FINAL(your best answer).")
    final = extract_final(answer, session)
    return StrategyRun(
        answer=final if final is not None else answer,
        iterations=_REPL_MAX_ITERATIONS,
        error="out of turns",
        notes=notes,
    )


async def repl(task: Task, ask: Ask) -> StrategyRun:
    """RLM at depth 0: context as a variable, code to inspect it, no sub-calls."""
    return await _run_repl(task, ask, system_prompt=_load("repl_system"), recursive=False)


async def repl_recursive(task: Task, ask: Ask) -> StrategyRun:
    """RLM at depth 1: the same, plus llm_query() callable from inside loops."""
    return await _run_repl(task, ask, system_prompt=_load("repl_recursive_system"), recursive=True)


STRATEGIES: dict[str, Strategy] = {
    "raw": raw,
    "truncate": truncate,
    "compact": compact,
    "repl": repl,
    "repl_recursive": repl_recursive,
}
