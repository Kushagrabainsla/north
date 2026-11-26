"""North keeps what it finds in files, marked unconfirmed until the user states it (issue #82, item 9).

Asked to store facts from a scan of local files, north saved 3 facts, all from the
user's own sentences, and nothing from the files: no tool let a run write a fact.
Three days later north still stated a fact the scan had shown to be out of date.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config.approval_mode import ApprovalMode
from memory import ContextDocument, FileContextStore
from memory.facts import FactStore
from tests.conftest import approving_store, bind_approvals
from tools.models import ToolInput
from tools.universal.remember import RememberTool
from utils.tasks import drain

_RESUME = "/Users/me/Documents/resume-2026.pdf"


async def _no_embeddings(texts: list[str]) -> list[list[float]]:
    raise RuntimeError("embedding model unavailable")


@pytest.fixture
async def memory(tmp_path: Path) -> tuple[FactStore, FileContextStore]:
    facts = FactStore(tmp_path / "facts.db", _no_embeddings)
    documents = FileContextStore(tmp_path / "context")
    await facts.add_fact("The user works at Initech.", about="current job", said_at=datetime(2026, 9, 1, tzinfo=UTC))
    return facts, documents


def _tool(memory, mode: ApprovalMode = ApprovalMode.YOLO) -> RememberTool:
    facts, documents = memory
    return bind_approvals(RememberTool(facts, documents), mode=mode, store=approving_store())


async def _current(facts: FactStore) -> dict[str, dict]:
    return {fact["content"]: fact for fact in await facts.all_facts() if fact["status"] == "active"}


async def test_a_fact_found_in_a_file_replaces_the_old_one_and_is_unconfirmed(memory) -> None:
    facts, documents = memory

    out = await _tool(memory).execute(
        ToolInput(
            params={
                "fact": "The user works at Acme as a data engineer.",
                "about": "current job",
                "source": _RESUME,
                "quote": "Data Engineer, Acme (2026-)",
                "task_id": "scan",
            }
        )
    )

    assert out.success, out.error
    current = await _current(facts)
    assert set(current) == {"The user works at Acme as a data engineer."}
    assert current["The user works at Acme as a data engineer."]["confirmed"] == 0
    recalled = await facts.search("where does the user work")
    assert any("unconfirmed: north read it in " + _RESUME in line for line in recalled), recalled
    profile = await documents.read(ContextDocument.USER)
    assert "unconfirmed" in profile and "Acme" in profile and "Initech" not in profile


async def test_the_user_stating_it_confirms_it(memory) -> None:
    facts, _ = memory
    tool = _tool(memory)
    found = {"fact": "The user lives in Oakland.", "about": "home city", "source": _RESUME, "task_id": "scan"}
    await tool.execute(ToolInput(params=found))

    await facts.add_fact("The user lives in Oakland.", about="home city")

    assert (await _current(facts))["The user lives in Oakland."]["confirmed"] == 1
    assert not any("unconfirmed" in line for line in await facts.search("where does the user live"))


async def test_a_fact_the_user_said_is_kept_as_confirmed(memory) -> None:
    facts, _ = memory

    out = await _tool(memory).execute(
        ToolInput(params={"fact": "The user is vegetarian.", "about": "diet", "said_by_user": True, "task_id": "c"})
    )

    assert out.success, out.error
    assert (await _current(facts))["The user is vegetarian."]["confirmed"] == 1


async def test_a_found_fact_needs_its_source(memory) -> None:
    out = await _tool(memory).execute(
        ToolInput(params={"fact": "The user has a cat.", "about": "pets", "task_id": "scan"})
    )

    assert not out.success
    assert "source" in out.error


async def test_finding_a_fact_again_does_not_unconfirm_what_the_user_said(memory) -> None:
    facts, _ = memory
    await facts.add_fact("The user plays chess.", about="hobby")

    await _tool(memory).execute(
        ToolInput(params={"fact": "The user plays chess.", "about": "hobby", "source": _RESUME, "task_id": "scan"})
    )

    assert (await _current(facts))["The user plays chess."]["confirmed"] == 1


async def test_remembering_goes_to_the_approval_layer(memory) -> None:
    facts, documents = memory
    store = approving_store()
    tool = bind_approvals(RememberTool(facts, documents), mode=ApprovalMode.ASK, store=store)

    await tool.execute(
        ToolInput(params={"fact": "The user runs marathons.", "about": "sport", "source": _RESUME, "task_id": "t"})
    )

    card = store.add.call_args.args[0].message
    assert "The user runs marathons." in card
    assert "unconfirmed" in card


# Issue #88: the task waits for `remember`, and the check for older facts asks a model
# (13 s on average in north's ledger). So the tool stores the fact and answers, and the
# check runs in the background.

_OLD = "The user interns at Initech."
_NEW = "The user's internship at Initech has ended."
_LATER = "The user interns at Globex."
# Similarity 0.75 between each pair: inside the band the check looks at (0.60 to 0.85).
_VECTORS = {_OLD: [1.0, 0.0, 0.0], _NEW: [0.75, 0.6614, 0.0], _LATER: [0.75, 0.0, 0.6614]}


async def _vectors(texts: list[str]) -> list[list[float]]:
    return [_VECTORS.get(text, [0.0, 0.0, 1.0]) for text in texts]


class _GatedCheck:
    """A contradiction check that, for the remembered fact, waits until the test lets it answer."""

    def __init__(self, fail: bool = False) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.seen: list[list[str]] = []
        self.fail = fail

    async def __call__(self, new_fact: str, candidates: list[str]) -> list[int]:
        if new_fact != _NEW:
            return []
        self.seen.append(candidates)
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("model unavailable")
        return list(range(len(candidates)))


async def _remember_new(tmp_path: Path, check: _GatedCheck) -> tuple[FactStore, FileContextStore, object]:
    facts = FactStore(tmp_path / "facts.db", _vectors, supersede_fn=check)
    documents = FileContextStore(tmp_path / "context")
    await facts.add_fact(_OLD)  # no label, as the facts of the September file scan
    tool = bind_approvals(RememberTool(facts, documents), mode=ApprovalMode.YOLO, store=approving_store())
    out = await tool.execute(
        ToolInput(params={"fact": _NEW, "about": "internship", "said_by_user": True, "task_id": "t"})
    )
    return facts, documents, out


async def test_the_task_does_not_wait_for_the_model_check(tmp_path: Path) -> None:
    check = _GatedCheck()

    facts, documents, out = await _remember_new(tmp_path, check)

    # The tool answered while the check is still waiting for its model.
    assert out.success, out.error
    assert set(await _current(facts)) == {_OLD, _NEW}
    assert _NEW in await documents.read(ContextDocument.USER)
    await asyncio.wait_for(check.started.wait(), timeout=2)

    check.release.set()
    await drain()

    assert set(await _current(facts)) == {_NEW}
    assert check.seen == [[_OLD]]
    assert _OLD not in await documents.read(ContextDocument.USER)


async def test_the_check_never_retires_a_fact_written_after_the_remembered_one(tmp_path: Path) -> None:
    check = _GatedCheck()
    facts, _, _ = await _remember_new(tmp_path, check)
    await asyncio.wait_for(check.started.wait(), timeout=2)

    await facts.add_fact(_LATER)
    check.release.set()
    await drain()

    assert set(await _current(facts)) == {_NEW, _LATER}
    assert check.seen == [[_OLD]]


async def test_a_failed_check_keeps_the_new_fact_and_the_old_one(tmp_path: Path) -> None:
    check = _GatedCheck(fail=True)
    facts, _, out = await _remember_new(tmp_path, check)

    check.release.set()
    await drain()

    assert out.success, out.error
    assert set(await _current(facts)) == {_OLD, _NEW}


async def test_a_fact_already_known_starts_no_check(tmp_path: Path) -> None:
    check = _GatedCheck()
    facts, _, _ = await _remember_new(tmp_path, check)
    check.release.set()
    await drain()
    tool = bind_approvals(RememberTool(facts, FileContextStore(tmp_path / "c2")), store=approving_store())

    out = await tool.execute(
        ToolInput(params={"fact": _NEW, "about": "internship", "said_by_user": True, "task_id": "t"})
    )
    await drain()

    assert out.success and out.data["new"] is False
    assert len(check.seen) == 1


async def test_a_writer_that_checks_at_once_still_does(tmp_path: Path) -> None:
    calls: list[str] = []

    async def check(new_fact: str, candidates: list[str]) -> list[int]:
        calls.append(new_fact)
        return [0]

    facts = FactStore(tmp_path / "facts.db", _vectors, supersede_fn=check)
    await facts.add_fact(_OLD)

    await facts.add_fact(_NEW, about="internship")

    assert calls == [_NEW]
    assert set(await _current(facts)) == {_NEW}
