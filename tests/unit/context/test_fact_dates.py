"""A fact knows when it is true, and one whose last day has passed stops reaching prompts.

North read an internship offer in September and kept "the internship is full-time"
as a bare present-tense sentence two months after it ended. Dates fix that: recall
says when each fact holds, an ended fact drops out, and a newer fact in any topic
can retire an older one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory import facts as fact_module
from memory.facts import FactStore, _with_dates

PAST = "2020-01-01"
FUTURE = "2099-12-31"
_NEW = [1.0, 0.0]
# Cosine 0.70 with _NEW: about the same thing, not the same claim.
_RELATED = [0.7, 0.714]


def _embedder(vectors: dict[str, list[float]] | None = None, default: list[float] | None = None):
    async def embed(texts: list[str]) -> list[list[float]]:
        return [(vectors or {}).get(t, default if default is not None else [1.0, 0.0]) for t in texts]

    return embed


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "facts.db"


def test_recall_says_when_a_fact_is_true() -> None:
    assert _with_dates("Interns at X", "2026-05-26", "2026-08-14", None) == (
        "Interns at X (true from May 26, 2026 until Aug 14, 2026)"
    )
    assert _with_dates("Offer stands", None, FUTURE, None) == "Offer stands (true until Dec 31, 2099)"
    assert _with_dates("Lives in Austin", PAST, None, None) == "Lives in Austin (true since Jan 1, 2020)"
    assert _with_dates("Starts at Acme", FUTURE, None, None) == "Starts at Acme (true from Dec 31, 2099)"
    # No dates: when it was learned is the next best clue.
    assert _with_dates("Likes tea", None, None, "2026-09-05T17:00:00+00:00").startswith("Likes tea (noted Sep ")
    assert _with_dates("Likes tea", None, None, None) == "Likes tea"


async def test_an_ended_fact_is_not_recalled_and_a_current_one_carries_its_dates(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[1.0, 0.0]))
    await store.add_fact("User interns at LinkedIn", category="jobs", valid_from="2026-05-26", valid_until=PAST)
    await store.add_fact("User studies at SJSU", category="education", valid_until=FUTURE)

    recalled = await store.search("where does the user work or study")

    assert recalled == ["User studies at SJSU (true until Dec 31, 2099)"]


async def test_an_ended_fact_leaves_the_recency_fallback_and_the_profile(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))  # no vectors: recall falls back to recency
    await store.add_fact("User interns at LinkedIn", category="jobs", valid_until=PAST)
    await store.add_fact("User prefers tea", category="preferences")

    recalled = await store.search("anything")
    profile = await store.render_profile()

    assert [fact.split(" (")[0] for fact in recalled] == ["User prefers tea"]
    assert "LinkedIn" not in profile and "User prefers tea" in profile


async def test_the_facts_tab_still_lists_an_ended_fact_marked_ended(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))
    await store.add_fact("User interns at LinkedIn", category="jobs", valid_until=PAST)
    await store.add_fact("User prefers tea", category="preferences", valid_until=FUTURE)

    listed = {fact["content"]: fact for fact in await store.all_facts()}

    assert listed["User interns at LinkedIn"]["ended"] is True
    assert listed["User prefers tea"]["ended"] is False
    assert listed["User prefers tea"]["valid_until"] == FUTURE


async def test_only_real_days_are_stored(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))
    await store.add_fact("User likes trains", valid_from="next Monday", valid_until="2026-02-30")

    (fact,) = await store.all_facts()

    assert (fact["valid_from"], fact["valid_until"]) == (None, None)


async def test_restating_a_fact_with_its_end_date_adds_the_date(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))
    await store.add_fact("User interns at LinkedIn", category="jobs")

    assert await store.add_fact("User interns at LinkedIn", category="jobs", valid_until=PAST) is False
    (fact,) = await store.all_facts()
    assert fact["ended"] is True


async def test_a_new_fact_in_one_topic_retires_a_contradicted_fact_in_another(db_path: Path) -> None:
    """The bug: "I finished my internship" (filed under user) never met "interns at X" (filed under jobs)."""
    asked: list[list[str]] = []

    async def supersede(new_fact: str, candidates: list[str]) -> list[int]:
        asked.append(candidates)
        return [0]

    vectors = {"User interns at LinkedIn": _RELATED, "User finished the LinkedIn internship": _NEW}
    store = FactStore(db_path=db_path, embed_fn=_embedder(vectors), supersede_fn=supersede)
    await store.add_fact_with_provenance("User interns at LinkedIn", category="jobs")

    await store.add_fact("User finished the LinkedIn internship", category="user")

    assert asked == [["User interns at LinkedIn"]]
    statuses = {fact["content"]: fact["status"] for fact in await store.all_facts()}
    assert statuses == {"User interns at LinkedIn": "superseded", "User finished the LinkedIn internship": "active"}


async def test_older_facts_get_the_dates_their_text_states_once(db_path: Path) -> None:
    calls: list[list[tuple[str, str]]] = []

    async def dates(facts: list[tuple[str, str]]) -> dict[int, tuple[str | None, str | None]]:
        calls.append(facts)
        found: dict[int, tuple[str | None, str | None]] = {}
        for index, (content, _) in enumerate(facts):
            if "May 26 through August 14" in content:
                found[index] = ("2026-05-26", "2026-08-14")
            if "backwards" in content:
                found[index] = ("2026-08-14", "2026-05-26")
        return found

    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]), dates_fn=dates)
    # Written the way reading your files writes them: without considering dates.
    await store.add_fact_with_provenance("The internship runs May 26 through August 14, 2026", category="schedule")
    await store.add_fact_with_provenance("User prefers tea", category="preferences")
    await store.add_fact_with_provenance("A range read backwards", category="other")
    await store.add_fact("User likes trains")  # extraction already considered its dates

    assert await store.date_facts() == 1
    assert await store.date_facts() == 0

    assert len(calls) == 1 and len(calls[0]) == 3  # once per fact, and not the one extraction dated
    listed = {fact["content"]: fact for fact in await store.all_facts()}
    assert (listed["The internship runs May 26 through August 14, 2026"]["valid_until"]) == "2026-08-14"
    assert listed["A range read backwards"]["valid_from"] is None


async def test_a_failed_dating_call_leaves_the_facts_for_next_time(db_path: Path) -> None:
    async def failing(facts):
        raise RuntimeError("model unavailable")

    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]), dates_fn=failing)
    await store.add_fact_with_provenance("The internship runs May 26 through August 14, 2026", category="schedule")

    assert await store.date_facts() == 0
    assert store._undated_sync()  # still waiting to be dated


async def test_editing_a_fact_can_set_and_clear_its_dates(db_path: Path) -> None:
    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]))
    await store.add_fact("User interns at LinkedIn", category="jobs")
    (fact,) = await store.all_facts()

    await store.update_fact(fact["id"], fact["content"], "jobs", dates=("2026-05-26", "2026-08-14"))
    assert (await store.all_facts())[0]["valid_until"] == "2026-08-14"

    await store.update_fact(fact["id"], fact["content"], "jobs")  # no dates given: left as they are
    assert (await store.all_facts())[0]["valid_until"] == "2026-08-14"

    await store.update_fact(fact["id"], fact["content"], "jobs", dates=(None, None))
    assert (await store.all_facts())[0]["valid_until"] is None


def test_a_day_is_the_settings_zone_day(monkeypatch) -> None:
    """ "Today" decides what has ended, so it is the day in the zone set on the Settings page."""
    import utils.time as north_time

    monkeypatch.setattr(north_time, "_configured_timezone_name", None)
    north_time.configure_timezone("Pacific/Kiritimati")  # UTC+14: already tomorrow almost everywhere
    kiritimati = fact_module._today()
    north_time.configure_timezone("Pacific/Pago_Pago")  # UTC-11
    pago_pago = fact_module._today()

    assert kiritimati > pago_pago


async def test_facts_from_one_document_are_dated_together(db_path: Path, monkeypatch) -> None:
    """A fact borrows its document's dates only when the model sees both in one batch."""
    monkeypatch.setattr(fact_module, "_DATES_BATCH", 2)
    batches: list[list[str]] = []

    async def dates(facts):
        batches.append([content for content, _ in facts])
        return {}

    store = FactStore(db_path=db_path, embed_fn=_embedder(default=[]), dates_fn=dates)
    for content, source in [
        ("offer a", "offer.pdf"),
        ("resume a", "resume.pdf"),
        ("offer b", "offer.pdf"),
        ("resume b", "resume.pdf"),
    ]:
        await store.add_fact_with_provenance(content, category="jobs", source_path=source)

    await store.date_facts()

    assert batches == [["offer a", "offer b"], ["resume a", "resume b"]]
