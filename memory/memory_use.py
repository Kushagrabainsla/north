"""Which of the memory a task was given did it use? (#85)

North recalls facts, past-task notes and the profile at the start of a task, but
it did not know which of them a task used, so no memory change could be measured.
After a run ends, one small background call marks each item "used" or "not used",
and the run's record gets a `memory_used` event.

How the judge was chosen (experiments/memory_recall, 120 replayed tasks, 2,950
items, against a Opus 5.5 reference whose own repeat agreed at kappa 0.92):
Haiku 5.5 at low effort agreed at kappa 0.58, with no model call (embedding or
word overlap) about 0.4. Asking the judge to quote the words of the answer that
show each use lifted Haiku 5.5 at medium effort to kappa 0.83, at a fraction of a
cent a task. A quote that is not in the answer or the actions does not count.

The call is background work, so it runs on the model the user chose for that.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Protocol

from inference.models import CompletionRequest
from memory.models import MemoryItem
from utils.prompts import load_prompt
from utils.tasks import spawn
from utils.text import extract_json

logger = logging.getLogger(__name__)

COMPONENT = "memory_use"
EVENT = "memory_used"
_MAX_ANSWER_CHARS = 6000
_MAX_TASK_CHARS = 3000
_MAX_ACTIONS = 40
_MAX_ACTION_CHARS = 300
_MAX_SECTION_CHARS = 1500

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "used": {"type": "boolean"}, "quote": {"type": "string"}},
                "required": ["id", "used", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


class _Router(Protocol):
    async def complete(self, request: CompletionRequest) -> Any: ...


class _Runs(Protocol):
    async def list_events(self, run_id: str) -> list[dict[str, Any]]: ...
    async def record_event(self, run_id: str, task_id: str, event: str, data: dict[str, Any]) -> None: ...


class _Counts(Protocol):
    async def record(self, loaded: Any, used: Any) -> None: ...


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def quote_found(quote: str, haystack: str) -> bool:
    """True when the quote (at least 3 words) is in *haystack*, allowing small copy slips."""
    words = _words(quote)
    if len(words) < 3:
        return False
    target = " ".join(_words(haystack))
    if " ".join(words) in target:
        return True
    windows = [" ".join(words[i : i + 4]) for i in range(max(1, len(words) - 3))]
    return sum(window in target for window in windows) / len(windows) >= 0.8


def _actions(events: list[dict[str, Any]]) -> list[str]:
    out = []
    for event in events:
        if event.get("event") != "tool_called":
            continue
        data = event.get("data") or {}
        params = str(data.get("params", ""))[:_MAX_ACTION_CHARS]
        out.append(f"{data.get('tool', '')}: {params}")
    return out[:_MAX_ACTIONS]


def build_prompt(items: list[MemoryItem], task: str, actions: list[str], answer: str) -> str:
    lines = [f"[{item.id}] {item.text[:_MAX_SECTION_CHARS]}" for item in items]
    return load_prompt("prompts/memory_use.md").format(
        items="\n".join(lines),
        task=task[:_MAX_TASK_CHARS],
        actions="\n".join(f"- {a}" for a in actions) or "(none)",
        answer=answer[:_MAX_ANSWER_CHARS],
    )


def summarize(items: list[MemoryItem], verdicts: dict[str, bool]) -> dict[str, Any]:
    """The event body: ids used and not used, and counts per kind of item."""
    loaded = Counter(item.kind for item in items)
    used = Counter(item.kind for item in items if verdicts.get(item.id))
    return {
        "used": [item.id for item in items if verdicts.get(item.id)],
        "not_used": [item.id for item in items if not verdicts.get(item.id)],
        "by_kind": {kind: {"loaded": loaded[kind], "used": used[kind]} for kind in sorted(loaded)},
    }


async def judge(
    router: _Router,
    runs: _Runs,
    *,
    run_id: str,
    task_id: str,
    task: str,
    answer: str,
    items: list[MemoryItem],
    counts: _Counts | None = None,
) -> dict[str, Any] | None:
    """Judge one finished run and record a memory_used event. Returns the event body, or None."""
    if not items or not answer.strip():
        return None
    actions = _actions(await runs.list_events(run_id))
    response = await router.complete(
        CompletionRequest(
            prompt=build_prompt(items, task, actions, answer),
            component=COMPONENT,
            task_id=task_id,
            max_tokens=8000,
            response_schema=_SCHEMA,
        )
    )
    parsed = extract_json(response.text)
    rows = parsed.get("items", []) if isinstance(parsed, dict) else []
    haystack = answer[:_MAX_ANSWER_CHARS] + "\n" + "\n".join(actions)
    known = {item.id for item in items}
    verdicts = {
        str(row.get("id")): bool(row.get("used")) and quote_found(str(row.get("quote", "")), haystack)
        for row in rows
        if isinstance(row, dict) and row.get("id") in known
    }
    body = {**summarize(items, verdicts), "model": response.model_used, "judged": len(verdicts)}
    await runs.record_event(run_id, task_id, EVENT, body)
    if counts is not None and verdicts:
        await counts.record([item.id for item in items], body["used"])
    return body


def judge_later(
    router: _Router | None,
    runs: _Runs | None,
    *,
    run_id: str | None,
    task_id: str,
    task: str,
    answer: str,
    items: list[MemoryItem],
    counts: _Counts | None = None,
) -> None:
    """Judge a finished run in the background; the run never waits for it."""
    if router is None or runs is None or not run_id or not items:
        return
    spawn(
        judge(router, runs, run_id=run_id, task_id=task_id, task=task, answer=answer, items=items, counts=counts),
        name=f"memory_use:{run_id}",
        logger=logger,
    )
