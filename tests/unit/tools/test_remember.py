"""North keeps what it finds in files, marked unconfirmed until the user states it (issue #82, item 9).

Asked to store facts from a scan of local files, north saved 3 facts, all from the
user's own sentences, and nothing from the files: no tool let a run write a fact.
Three days later north still stated a fact the scan had shown to be out of date.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from config.approval_mode import ApprovalMode
from memory import ContextDocument, FileContextStore
from memory.facts import FactStore
from tests.conftest import approving_store, bind_approvals
from tools.models import ToolInput
from tools.universal.remember import RememberTool

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
