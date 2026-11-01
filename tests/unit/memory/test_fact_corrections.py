"""A correction replaces the fact it corrects: one current value per thing a fact is about.

In the issue #76 test (finding M8) the user said "im allergic to penicillin",
then "wait im not allergic to penicillin, i mixed it up, its amoxicillin". Both
"User is allergic to penicillin" and "User is not allergic to penicillin." stayed
active, as did both dogs and both employers, while north replied "I've updated
the record". Facts were free text: noticing that two of them contradict needed
an embedding (the model could not load that day) and a model call. Now each fact
the user states is filed under what it is about, and the newest thing said about
it is its one current value.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import FileContextStore
from memory.extraction import ExtractionPipeline
from memory.facts import FactStore

_T0 = datetime(2026, 10, 31, 1, 0, tzinfo=UTC)


async def _no_embeddings(texts: list[str]) -> list[list[float]]:
    raise RuntimeError("Could not load the local embedding model BAAI/bge-small-en-v1.5 (403 Forbidden)")


async def _all_alike(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0, 0.0] for _ in texts]


@pytest.fixture
def store(tmp_path: Path) -> FactStore:
    return FactStore(tmp_path / "facts.db", _no_embeddings)


async def _current(store: FactStore) -> set[str]:
    return {fact["content"] for fact in await store.all_facts() if fact["status"] == "active"}


async def test_a_correction_leaves_one_current_value(store: FactStore) -> None:
    await store.add_fact("User is allergic to penicillin", about="penicillin allergy", said_at=_T0)
    later = _T0 + timedelta(minutes=20)
    await store.add_fact("User is not allergic to penicillin.", about="Penicillin allergy", said_at=later)
    await store.add_fact("User is allergic to amoxicillin.", about="amoxicillin allergy", said_at=later)

    assert await _current(store) == {"User is not allergic to penicillin.", "User is allergic to amoxicillin."}
    old = next(f for f in await store.all_facts() if f["content"] == "User is allergic to penicillin")
    assert old["status"] == "superseded"


async def test_a_new_dog_and_a_new_job_replace_the_old_ones(store: FactStore) -> None:
    await store.add_fact("User's name is Sam", about="name", said_at=_T0)
    await store.add_fact("User works as a nurse at Valley Medical", about="employer", said_at=_T0)
    await store.add_fact("User's dog is called Biscuit", about="dog's name", said_at=_T0)
    later = _T0 + timedelta(minutes=10)
    await store.add_fact("User's dog is called Mango.", about="dog's name", said_at=later)
    await store.add_fact("User works at St. Lukes.", about="employer", said_at=later)

    assert await _current(store) == {"User's name is Sam", "User's dog is called Mango.", "User works at St. Lukes."}


async def test_saying_an_old_value_again_makes_it_current_again(store: FactStore) -> None:
    await store.add_fact("User is allergic to penicillin", about="penicillin allergy", said_at=_T0)
    await store.add_fact("User is not allergic to penicillin", about="penicillin allergy", said_at=_T0 + timedelta(1))
    await store.add_fact("User is allergic to penicillin", about="penicillin allergy", said_at=_T0 + timedelta(2))

    assert await _current(store) == {"User is allergic to penicillin"}


async def test_the_newest_thing_said_wins_whatever_order_it_is_stored_in(store: FactStore) -> None:
    # Extraction reads a backlog several messages at a time.
    await store.add_fact("User is not allergic to penicillin.", about="penicillin allergy", said_at=_T0 + timedelta(1))
    await store.add_fact("User is allergic to penicillin", about="penicillin allergy", said_at=_T0)

    assert await _current(store) == {"User is not allergic to penicillin."}


async def test_facts_about_different_things_never_merge_however_alike_they_read(tmp_path: Path) -> None:
    store = FactStore(tmp_path / "facts.db", _all_alike)
    await store.add_fact("User is allergic to penicillin.", about="penicillin allergy", said_at=_T0)
    await store.add_fact("User is allergic to amoxicillin.", about="amoxicillin allergy", said_at=_T0)

    assert await store.deduplicate() == 0
    assert await _current(store) == {"User is allergic to penicillin.", "User is allergic to amoxicillin."}


async def test_a_fact_from_before_labels_takes_one_when_restated(store: FactStore) -> None:
    await store.add_fact("User works at Acme.")
    await store.add_fact("User works at Acme.", about="employer")
    await store.add_fact("User works at Initech.", about="employer")

    facts = await store.all_facts()
    assert len(facts) == 2
    assert {(f["content"], f["about"], f["status"]) for f in facts} == {
        ("User works at Acme.", "employer", "superseded"),
        ("User works at Initech.", "employer", "active"),
    }


async def test_labels_in_use_are_listed_most_recent_first(store: FactStore) -> None:
    await store.add_fact("User's dog is called Biscuit", about="dog's name", said_at=_T0)
    await store.add_fact("User works at St. Lukes.", about="employer", said_at=_T0 + timedelta(1))
    await store.add_fact("User likes hiking.")

    assert await store.abouts() == ["employer", "dog's name"]


def test_a_store_from_before_labels_gains_them(tmp_path: Path) -> None:
    path = tmp_path / "facts.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE context_facts (id TEXT NOT NULL PRIMARY KEY, content TEXT NOT NULL, "
            "category TEXT NOT NULL DEFAULT 'user', embedding TEXT, updated_at DATETIME NOT NULL)"
        )
        conn.execute("INSERT INTO context_facts VALUES ('f1', 'User is allergic to penicillin', 'user', '[]', 'x')")

    FactStore(path, _no_embeddings)

    with sqlite3.connect(path) as conn:
        assert "about" in {row[1] for row in conn.execute("PRAGMA table_info(context_facts)")}


class _Model:
    """Extraction's model, answering as Haiku 5.5 would for the run's two messages."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    async def complete(self, request) -> SimpleNamespace:
        self.prompts.append(request.prompt)
        return SimpleNamespace(text=json.dumps(self.replies[len(self.prompts) - 1]))


def _said(text: str, at: datetime) -> LedgerEntry:
    return LedgerEntry(id=text[:8], timestamp=at, source=LedgerSource.PROMPT, input=text, status=LedgerStatus.COMPLETED)


async def test_extraction_files_the_correction_under_the_label_it_corrects(tmp_path: Path, store: FactStore) -> None:
    model = _Model(
        [
            {
                "facts": [
                    {"document": "user", "delta": "User is allergic to penicillin.", "about": "penicillin allergy"}
                ]
            },
            {
                "facts": [
                    {"document": "user", "delta": "User is not allergic to penicillin.", "about": "penicillin allergy"},
                    {"document": "user", "delta": "User is allergic to amoxicillin.", "about": "amoxicillin allergy"},
                ]
            },
        ]
    )
    pipeline = ExtractionPipeline(
        AsyncMock(), FileContextStore(tmp_path / "context"), model, tmp_path, fact_store=store
    )

    await pipeline._process_entry(_said("also fyi my favourite food is pho and im allergic to penicillin", _T0))
    await pipeline._process_entry(
        _said("wait im not allergic to penicillin, i mixed it up, its amoxicillin", _T0 + timedelta(minutes=20))
    )

    # The second message is read with the label the first one made, so the correction can reuse it.
    assert "- penicillin allergy" in model.prompts[1]
    assert await _current(store) == {"User is not allergic to penicillin.", "User is allergic to amoxicillin."}


def test_a_labelled_fact_states_one_thing() -> None:
    """ "i work as a nurse at valley medical" became one fact labelled employer, and the job
    correction that replaced it erased "nurse" too."""
    prompt = (Path(__file__).parents[3] / "resources" / "prompts" / "fact_extraction.md").read_text(encoding="utf-8")

    assert '"User works as a nurse" (job title) and "User works at Valley\n  Medical" (employer)' in prompt
