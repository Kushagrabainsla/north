"""Step 0: how often could one running task have helped another one in north's own history?

Reads the ledger only (read-only), embeds task prompts with north's on-device
encoder, and makes no model call. A *reader* task B could use a lesson from a
*writer* task A when:

- they are related: prompt similarity in the top 10% of all task pairs;
- A took a model step while B was working (steps under 10 minutes apart) and
  B still had a step left to use it.

Two readings of "could use":

- instant: A publishes as it works and B re-reads before each step;
- today: B only reads when an agent run starts, and A's lesson exists only after
  A ends plus the consolidator and distiller polls.

Run:
    .venv/bin/python -m experiments.lesson_sharing.opportunity
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from inference.models import EmbedRequest
from inference.providers.local_embeddings import LocalEmbeddingProvider
from utils.math import cosine_similarity

_LEDGER = Path.home() / ".north" / "ledger.db"
_RESULTS = Path(__file__).with_name("opportunity_results.json")
# Episodes are polled every 120 s and skills distilled every 900 s, so a lesson
# from a finished task reaches a new agent run at the latest this long after.
_TODAY_DELAY = datetime.timedelta(seconds=120 + 900)
# A gap this long between two model steps means the task was waiting, not working.
_IDLE_GAP = datetime.timedelta(minutes=10)
_TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders", tempfile.gettempdir())
_BRIEFINGS = frozenset({"news-briefing", "research-paper-briefing"})
_PROMPT_SOURCES = ("prompt", "cron", "webhook", "manual_injection", "mic")


@dataclass
class Task:
    task_id: str
    prompt: str = ""
    workspace: str = ""
    steps: list[datetime.datetime] = field(default_factory=list)
    run_starts: list[datetime.datetime] = field(default_factory=list)
    skills: set[str] = field(default_factory=set)


def _when(stamp: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(stamp)


def load_tasks() -> list[Task]:
    """Tasks with at least two model steps, outside temp folders."""
    conn = sqlite3.connect(f"file:{_LEDGER}?mode=ro", uri=True)
    rows = conn.execute(
        "select task_id, timestamp, source, action, input, output, agent_output from ledger "
        "where task_id is not null order by timestamp"
    ).fetchall()
    conn.close()
    tasks: dict[str, Task] = {}
    for task_id, stamp, source, action, text, output, agent_output in rows:
        task = tasks.setdefault(task_id, Task(task_id))
        if action == "inference_call":
            task.steps.append(_when(stamp))
        elif action == "skill_selected":
            task.run_starts.append(_when(stamp))
            task.skills.update(name.strip() for name in (output or "").split(",") if name.strip())
        if source in _PROMPT_SOURCES and text and not task.prompt:
            task.prompt = text
        if agent_output and not task.workspace:
            try:
                workspace = json.loads(agent_output).get("workspace")
            except (ValueError, AttributeError):
                workspace = None
            if isinstance(workspace, str):
                task.workspace = workspace
    return [
        task
        for task in tasks.values()
        if len(task.steps) >= 2 and not any(task.workspace.startswith(root) for root in _TEMP_ROOTS if root)
    ]


async def embed_prompts(tasks: list[Task]) -> dict[str, list[float]]:
    texts = {task.task_id: task.prompt or " ".join(sorted(task.skills)) for task in tasks}
    keep = {task_id: text for task_id, text in texts.items() if text}
    provider = LocalEmbeddingProvider()
    response = await provider.embed("", EmbedRequest(texts=list(keep.values())))
    return dict(zip(keep, response.embeddings, strict=True))


def _active_spans(steps: list[datetime.datetime]) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """Stretches of work: steps no more than _IDLE_GAP apart (a wait for approval ends a stretch)."""
    spans = [(steps[0], steps[0])]
    for step in steps[1:]:
        start, end = spans[-1]
        if step - end <= _IDLE_GAP:
            spans[-1] = (start, step)
        else:
            spans.append((step, step))
    return spans


def _helped_while_working(writer: Task, reader: Task) -> bool:
    """A writer step falls inside a reader's stretch of work, with a reader step still to come."""
    return any(any(start <= step < end for step in writer.steps) for start, end in _active_spans(reader.steps))


def count(tasks: list[Task], vectors: dict[str, list[float]]) -> dict:
    by_id = {task.task_id: task for task in tasks}
    ids = [task.task_id for task in tasks if task.task_id in vectors]
    all_pairs = sorted(cosine_similarity(vectors[a], vectors[b]) for i, a in enumerate(ids) for b in ids[i + 1 :])
    # The encoder scores unrelated prompts around 0.6, so "related" is relative: the top 10% of all pairs.
    related_bar = all_pairs[int(len(all_pairs) * 0.9)]
    working_together, instant, instant_same_text, today = set(), set(), set(), set()
    for reader in tasks:
        for writer in tasks:
            if writer is reader or not _helped_while_working(writer, reader):
                continue
            working_together.add(reader.task_id)
            similarity = 0.0
            if reader.task_id in vectors and writer.task_id in vectors:
                similarity = cosine_similarity(vectors[reader.task_id], vectors[writer.task_id])
            if similarity < related_bar:
                continue
            instant.add(reader.task_id)
            if reader.prompt and reader.prompt == writer.prompt:
                instant_same_text.add(reader.task_id)
            # Today: a reader agent run starts after the writer's lesson exists.
            ready = writer.steps[-1] + _TODAY_DELAY
            if any(start >= ready for start in reader.run_starts):
                today.add(reader.task_id)
    return {
        "tasks": len(tasks),
        "first_task": min(task.steps[0] for task in tasks).isoformat(),
        "last_task": max(task.steps[-1] for task in tasks).isoformat(),
        "tasks_working_while_another_works": len(working_together),
        "related_bar_top10pct": round(related_bar, 3),
        "all_pair_similarity_p50": round(all_pairs[len(all_pairs) // 2], 3),
        "readers_reachable_instant": len(instant),
        "of_which_same_prompt_text": len(instant_same_text),
        "readers_reachable_today": len(today),
        "share_reachable_instant": round(len(instant) / len(tasks), 3),
        "share_reachable_today": round(len(today) / len(tasks), 3),
        # Counts only: prompts are the user's own words and stay out of the repository.
        "reachable_instant_briefing_flows": sum(
            not by_id[t].prompt and bool(by_id[t].skills & _BRIEFINGS) for t in instant
        ),
    }


def _percentiles(values: list[float]) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    return [ordered[len(ordered) // 2], ordered[int(len(ordered) * 0.9)], ordered[-1]]


def main() -> None:
    tasks = load_tasks()
    vectors = asyncio.run(embed_prompts(tasks))
    result = count(tasks, vectors)
    _RESULTS.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
