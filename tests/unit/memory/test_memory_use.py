"""Which recalled memory a task used (#85) and the use score in recall (#91)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from inference.models import CompletionRequest
from memory import ContextDocument, FileContextStore, LocalMemoryGateway, MemoryContext
from memory.facts import FactStore, _apply_recall_cutoff
from memory.memory_use import EVENT, judge, quote_found, summarize
from memory.models import MemoryItem
from memory.use_store import USE_WEIGHT, MemoryUseStore


class _Router:
    def __init__(self, verdicts: list[dict[str, Any]]) -> None:
        self.requests: list[CompletionRequest] = []
        self._text = json.dumps({"items": verdicts})

    async def complete(self, request: CompletionRequest) -> Any:
        self.requests.append(request)
        return type("Response", (), {"text": self._text, "model_used": "small-model"})()


class _Runs:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.recorded: list[tuple[str, str, str, dict[str, Any]]] = []

    async def list_events(self, run_id: str) -> list[dict[str, Any]]:
        return self._events

    async def record_event(self, run_id: str, task_id: str, event: str, data: dict[str, Any]) -> None:
        self.recorded.append((run_id, task_id, event, data))


ITEMS = [
    MemoryItem("fact:f1", "fact", "The user's dentist is Dr. Lee."),
    MemoryItem("fact:f2", "fact", "The user likes jazz."),
    MemoryItem("profile:Judgement rules", "profile", "## Judgement rules\n- Never book before 9am."),
]


def test_quote_must_be_in_the_answer() -> None:
    answer = "I booked the appointment with Dr. Lee for 10am on Friday."
    assert quote_found("appointment with Dr. Lee for 10am", answer)
    assert quote_found("the appointment with dr lee, for 10am", answer)  # case and punctuation slips
    assert not quote_found("booked a table at the jazz club", answer)
    assert not quote_found("Dr. Lee", answer)  # too short to show a use


async def test_judge_records_only_uses_that_the_answer_shows(tmp_path: Path) -> None:
    router = _Router(
        [
            {"id": "fact:f1", "used": True, "quote": "appointment with Dr. Lee for 10am"},
            # Claimed, but the quote is not in the answer or the actions: not counted.
            {"id": "fact:f2", "used": True, "quote": "you will enjoy the jazz playlist"},
            {"id": "profile:Judgement rules", "used": True, "quote": "calendar: 10:00 Friday dentist"},
        ]
    )
    runs = _Runs([{"event": "tool_called", "data": {"tool": "calendar", "params": "10:00 Friday dentist"}}])
    counts = MemoryUseStore(tmp_path / "use.db")

    body = await judge(
        router,
        runs,
        run_id="r1",
        task_id="t1",
        task="Book my dentist",
        answer="I booked the appointment with Dr. Lee for 10am on Friday.",
        items=ITEMS,
        counts=counts,
    )

    assert body is not None
    assert body["used"] == ["fact:f1", "profile:Judgement rules"]
    assert body["not_used"] == ["fact:f2"]
    assert body["by_kind"] == {"fact": {"loaded": 2, "used": 1}, "profile": {"loaded": 1, "used": 1}}
    assert runs.recorded[0][2] == EVENT
    assert router.requests[0].component == "memory_use"
    assert "calendar: 10:00 Friday dentist" in router.requests[0].prompt
    assert await counts.used_share() == {"fact": {"loaded": 2, "used": 1}, "profile": {"loaded": 1, "used": 1}}


async def test_judge_skips_a_run_with_no_memory_or_no_answer() -> None:
    router, runs = _Router([]), _Runs([])
    assert await judge(router, runs, run_id="r", task_id="t", task="x", answer="done", items=[]) is None
    assert await judge(router, runs, run_id="r", task_id="t", task="x", answer="  ", items=ITEMS) is None
    assert router.requests == [] and runs.recorded == []


def test_summarize_counts_per_kind() -> None:
    assert summarize(ITEMS, {"fact:f2": True})["by_kind"]["fact"] == {"loaded": 2, "used": 1}


def test_use_store_boosts_follow_the_use_rate(tmp_path: Path) -> None:
    store = MemoryUseStore(tmp_path / "use.db")
    store._record_sync(["fact:a", "fact:b"], {"fact:a"})
    store._record_sync(["fact:a", "fact:b"], {"fact:a"})
    boosts = store.boosts(["fact:a", "fact:b", "fact:new"])
    assert boosts["fact:new"] == 0.0  # never loaded: no change to recall
    assert boosts["fact:a"] == USE_WEIGHT * (3 / 4 - 0.5)
    assert boosts["fact:b"] == USE_WEIGHT * (1 / 4 - 0.5)


def test_cutoff_without_boosts_is_similarity_alone() -> None:
    # Best 0.80, margin 0.05: past the first three, a fact needs 0.75.
    scored = [("a", "A", 0.80), ("b", "B", 0.78), ("c", "C", 0.77), ("d", "D", 0.76), ("e", "E", 0.74)]
    assert [i for i, _ in _apply_recall_cutoff(scored, 15)] == ["a", "b", "c", "d"]
    assert _apply_recall_cutoff(scored, 15) == _apply_recall_cutoff(scored, 15, {})


def test_a_used_fact_moves_in_and_an_unused_one_moves_down() -> None:
    scored = [("a", "A", 0.80), ("b", "B", 0.78), ("c", "C", 0.77), ("d", "D", 0.74), ("e", "E", 0.73)]
    ranked = [i for i, _ in _apply_recall_cutoff(scored, 15, {"e": 0.05, "a": -0.05})]
    assert ranked[0] == "b"
    assert "e" in ranked


def _embed(vectors: dict[str, list[float]]):
    async def embed(texts: list[str]) -> list[list[float]]:
        return [vectors.get(t, [1.0, 0.0, 0.0]) for t in texts]

    return embed


async def test_recall_returns_ids_skips_facts_the_profile_shows_and_uses_boosts(tmp_path: Path) -> None:
    # Both facts match the question equally (0.8), and are not near-duplicates of each other (0.64).
    vectors = {
        "dentist question": [1.0, 0.0, 0.0],
        "The user's dentist is Dr. Lee": [0.8, 0.6, 0.0],
        "The user lives in Oakland": [0.8, 0.0, 0.6],
    }
    facts = FactStore(db_path=tmp_path / "facts.db", embed_fn=_embed(vectors))
    await facts.add_fact("The user's dentist is Dr. Lee")
    await facts.add_fact("The user lives in Oakland")
    by_text = {row["content"]: row["id"] for row in await facts.all_facts()}
    context = FileContextStore(tmp_path / "context")
    gateway = LocalMemoryGateway(context, facts, use_store=MemoryUseStore(tmp_path / "use.db"))
    principal = await gateway.principal_for("general", "general")

    recalled = await gateway.recall(principal, "dentist question")
    assert len(recalled.facts) == 2
    assert set(recalled.fact_ids) == set(by_text.values())

    # A profile line that shows a fact means that fact is not recalled a second time.
    shown = recalled.facts[1]
    await context.write(ContextDocument.USER, f"# User Profile\n\n## Other\n- {shown}")
    recalled = await gateway.recall(principal, "dentist question")
    assert len(recalled.facts) == 1 and shown not in recalled.facts
    assert len(recalled.fact_ids) == 1

    # A fact that tasks kept using ranks first; one they never used ranks last.
    oakland, dentist = by_text["The user lives in Oakland"], by_text["The user's dentist is Dr. Lee"]
    await context.write(ContextDocument.USER, "")
    use = gateway.use_store
    assert use is not None
    for _ in range(5):
        await use.record([f"fact:{oakland}", f"fact:{dentist}"], [f"fact:{oakland}"])
    assert (await gateway.recall(principal, "dentist question")).fact_ids == [oakland, dentist]


def test_item_ids_hold_no_memory_text() -> None:
    context = MemoryContext(
        facts=["private fact"],
        fact_ids=["f9"],
        documents=["private profile with no heading", "# User Profile\n\n## Health\n- private"],
    )
    ids = [item.id for item in context.items()]
    assert ids == ["fact:f9", "profile:#0.0", "profile:User Profile", "profile:Health"]
    assert "private" not in str(context.telemetry())
