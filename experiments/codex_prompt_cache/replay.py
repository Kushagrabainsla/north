"""Offline replay: how much of each run's first call a shared cache session could reuse.

The probe showed Codex caches the tool list ahead of the instructions, and stops at
the first tool that differs. So this rebuilds each past general-agent run's
first-call tool set the way `Agent._load_tools` picks it (hybrid top-8, the
essential tools, tools named in the task) from a copy of the tool index, and
counts what the run before it under the same session key had already cached:
the shared run of sorted tools, plus the instructions when every tool matches.

No model is called. Reads ~/.north/tasks/tasks.db and ~/.north/tool_index.db
(copied first, so the live index is never touched).

Run:
    .venv/bin/python -m experiments.codex_prompt_cache.replay --since 2026-10-17 --until 2026-10-22T19:10
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agents.agentic_llm_agent import _estimate_value_tokens
from agents.base import _ESSENTIAL_TOOL_NAMES, _tool_names_in_text
from config.settings import settings
from experiments.codex_prompt_cache.probe import function_schema
from experiments.tool_selection.retrieval_benchmark import discover_tool_docs
from inference.models import EmbedRequest
from inference.providers.local_embeddings import DEFAULT_MODEL_ID, LocalEmbeddingProvider
from tools.tool_index import SEMANTIC_TOP_K, ToolIndex

# OpenAI serves no cache for a shared prefix under 1,024 tokens.
MIN_CACHED_TOKENS = 1024
# A tool the docs no longer describe (removed since the run) counts as an average one.
_UNKNOWN_TOOL_TOKENS = 300


@dataclass(frozen=True)
class RunTools:
    """One past run's first call: when it started, its tools, and its prompt sizes."""

    started: datetime
    task_id: str
    tools: tuple[str, ...]
    instructions_tokens: int
    first_call_tokens: int


@dataclass(frozen=True)
class KeyEstimate:
    """What one session-key choice would have served from cache on first calls."""

    cached_tokens: int
    first_call_tokens: int
    runs_helped: int


def shared_prefix_tokens(
    previous: RunTools, current: RunTools, tool_tokens: Mapping[str, int], min_cached: int = MIN_CACHED_TOKENS
) -> int:
    """Tokens of *current*'s first call already cached by *previous*: equal leading tools, then instructions."""
    shared = 0
    for before, now in zip(previous.tools, current.tools, strict=False):
        if before != now:
            break
        shared += tool_tokens.get(now, _UNKNOWN_TOOL_TOKENS)
    if previous.tools == current.tools:
        shared += min(previous.instructions_tokens, current.instructions_tokens)
    return shared if shared >= min_cached else 0


def estimate_reuse(
    runs: Sequence[RunTools],
    session_key: Callable[[RunTools], str],
    ttl_minutes: float,
    tool_tokens: Mapping[str, int],
) -> KeyEstimate:
    """Replay *runs* in order, each reusing the last run under its key if that ran within the cache life."""
    last: dict[str, RunTools] = {}
    cached = helped = 0
    for run in runs:
        key = session_key(run)
        previous = last.get(key)
        if previous is not None and (run.started - previous.started).total_seconds() <= ttl_minutes * 60:
            reused = shared_prefix_tokens(previous, run, tool_tokens)
            cached += reused
            helped += reused > 0
        last[key] = run
    return KeyEstimate(cached, sum(run.first_call_tokens for run in runs), helped)


SESSION_KEYS: dict[str, Callable[[RunTools], str]] = {
    "per run (today)": lambda run: f"{run.started.isoformat()}:{run.task_id}",
    "per task": lambda run: run.task_id,
    "per agent": lambda run: "general",
}


def tool_token_sizes() -> dict[str, int]:
    """Estimated prompt tokens of each tool's schema, counted as prompt_profile counts them."""
    return {name: _estimate_value_tokens(function_schema(doc)) for name, doc in discover_tool_docs().items()}


def _runs(tasks_db: Path, since: str, until: str) -> list[tuple[str, str, str, int, int]]:
    query = """
        SELECT r.started_at, r.task_id, r.prompt,
               json_extract(p.data, '$.sections.system_instructions'),
               json_extract(p.data, '$.actual_input_tokens')
        FROM agent_runs r
        JOIN agent_run_events p ON p.id = (
            SELECT id FROM agent_run_events
            WHERE run_id = r.run_id AND event = 'prompt_profile' ORDER BY id LIMIT 1)
        WHERE r.agent = 'general' AND r.started_at >= ? AND r.started_at < ?
        ORDER BY r.started_at
    """
    with sqlite3.connect(tasks_db) as conn:
        return conn.execute(query, (since, until)).fetchall()


async def rebuild_runs(tasks_db: Path, tool_index_db: Path, since: str, until: str) -> list[RunTools]:
    """Each run's first-call tool set, re-selected from a throwaway copy of the tool index."""
    provider = LocalEmbeddingProvider()

    async def embed(texts: list[str]) -> list[list[float]]:
        return (await provider.embed(DEFAULT_MODEL_ID, EmbedRequest(texts=texts))).embeddings

    with tempfile.TemporaryDirectory() as scratch:
        index_copy = Path(scratch) / "tool_index.db"
        shutil.copy(tool_index_db, index_copy)
        with sqlite3.connect(index_copy) as conn:
            indexed = {row[0] for row in conn.execute("SELECT name FROM tool_embeddings")}
        index = ToolIndex(index_copy, embed, embedding_model=DEFAULT_MODEL_ID)
        runs = []
        for started, task_id, prompt, instructions_tokens, first_call_tokens in _runs(tasks_db, since, until):
            query = f"Agent role: general. Domain: general.\nTask: {prompt}"
            semantic = set(await index.search_tools(query, top_k=SEMANTIC_TOP_K)) & indexed
            chosen = semantic | (_ESSENTIAL_TOOL_NAMES & indexed) | _tool_names_in_text(prompt, indexed)
            runs.append(
                RunTools(
                    started=datetime.fromisoformat(started),
                    task_id=task_id,
                    tools=tuple(sorted(chosen)),
                    instructions_tokens=instructions_tokens or 0,
                    first_call_tokens=first_call_tokens or 0,
                )
            )
    return runs


def uncached_split(tasks_db: Path, since: str, until: str) -> dict[str, int]:
    """Uncached input tokens of first calls and of later calls, over general-agent tool calls."""
    query = """
        WITH calls AS (
            SELECT json_extract(e.data, '$.tokens_in') AS tokens_in,
                   json_extract(e.data, '$.cached_tokens') AS cached,
                   ROW_NUMBER() OVER (PARTITION BY e.run_id ORDER BY e.id) AS n
            FROM agent_run_events e JOIN agent_runs r ON r.run_id = e.run_id
            WHERE e.event = 'inference_call' AND r.agent = 'general'
              AND json_extract(e.data, '$.request_kind') = 'tool_completion'
              AND r.started_at >= ? AND r.started_at < ?)
        SELECT SUM(CASE WHEN n = 1 THEN tokens_in - cached END),
               SUM(CASE WHEN n > 1 THEN tokens_in - cached END),
               SUM(CASE WHEN n > 1 THEN cached END)
        FROM calls
    """
    with sqlite3.connect(tasks_db) as conn:
        first, later, later_cached = conn.execute(query, (since, until)).fetchone()
    return {"first_call_uncached": first or 0, "later_uncached": later or 0, "later_cached": later_cached or 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-10-17")
    parser.add_argument("--until", default="9999")
    parser.add_argument("--ttl", type=float, nargs="+", default=(5, 10, 60), help="Cache life in minutes.")
    args = parser.parse_args()
    home = settings.north_home
    tasks_db = home / "tasks" / "tasks.db"
    runs = asyncio.run(rebuild_runs(tasks_db, home / "tool_index.db", args.since, args.until))
    sizes = tool_token_sizes()
    same = sum(1 for before, now in zip(runs, runs[1:], strict=False) if before.tools == now.tools)
    print(f"{len(runs)} runs; same tool set as the run before: {same}/{len(runs) - 1}")
    split = uncached_split(tasks_db, args.since, args.until)
    total_uncached = split["first_call_uncached"] + split["later_uncached"]
    print(f"uncached input: first calls {split['first_call_uncached']:,} of {total_uncached:,}")
    print("session key      cache life  cached on first calls  share of all uncached  runs helped")
    for name, key in SESSION_KEYS.items():
        for ttl in args.ttl:
            estimate = estimate_reuse(runs, key, ttl, sizes)
            print(
                f"{name:<16} {ttl:>6.0f} min  {estimate.cached_tokens:>9,} / {estimate.first_call_tokens:,}"
                f"  {estimate.cached_tokens / total_uncached:>20.1%}  {estimate.runs_helped:>11}"
            )


if __name__ == "__main__":
    main()
