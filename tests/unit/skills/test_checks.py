"""A flow step's result is checked in code, against the source, before the run counts it done."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from skills import checks
from skills.checks import CheckSpec, parse_checks, run_checks
from skills.models import Skill, SkillExecution, SkillSource, skill_fingerprint
from skills.registry import parse_execution_contract

TODAY = date(2026, 10, 17)
# Oct 17's real briefing: the model wrote "ExecutionBridge: ..." for this paper.
ARXIV = {
    "2610.22041": (
        "Plans Are Not Executions: Adapting Frozen World-Model Planners to Controller-Induced Execution Shift",
        date(2026, 10, 15),
    ),
    "2610.22340": ("Dexterity from Supervised Play", date(2026, 10, 15)),
    "2603.04715": ("Probabilistic Dreaming for World Models", date(2026, 3, 5)),
}


@pytest.fixture
def arxiv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    asked: list[list[str]] = []

    def fake(ids: list[str]) -> dict:
        asked.append(ids)
        return {paper_id: ARXIV[paper_id] for paper_id in ids if paper_id in ARXIV}

    monkeypatch.setattr(checks, "_arxiv_metadata", fake)
    return asked


def _briefing(folder: Path, text: str, name: str = "2026-10-17.md") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


async def _check(path: Path, research: Path) -> list[str]:
    specs = (
        CheckSpec("saved_file", {"field": "file"}),
        CheckSpec("arxiv_papers", {"field": "file", "earlier": str(research)}),
    )
    return await run_checks(specs, {"file": str(path)}, today=TODAY)


async def test_a_briefing_true_to_arxiv_passes(tmp_path: Path, arxiv) -> None:
    research = tmp_path / "research"
    path = _briefing(research / "task-1", "### Dexterity from Supervised Play\n- https://arxiv.org/abs/2610.22340\n")

    assert await _check(path, research) == []
    assert arxiv == [["2610.22340"]]  # one request for every paper


async def test_a_made_up_title_is_caught_with_the_real_one(tmp_path: Path, arxiv) -> None:
    research = tmp_path / "research"
    path = _briefing(
        research / "task-1",
        "### ExecutionBridge: Adapting Frozen World-Model Planners to Controller-Induced Execution Shift\n"
        "- **Venue/status:** arXiv preprint (`arXiv:2610.22041`)\n",
    )

    assert await _check(path, research) == [
        "2610.22041: the title must be exactly arXiv's, 'Plans Are Not Executions: Adapting Frozen World-Model "
        "Planners to Controller-Induced Execution Shift'"
    ]


async def test_an_old_paper_a_repeat_and_an_unknown_id_are_each_named(tmp_path: Path, arxiv) -> None:
    research = tmp_path / "research"
    earlier = _briefing(
        research / "task-0", "Dexterity from Supervised Play https://arxiv.org/abs/2610.22340", "2026-10-16.md"
    )
    os.utime(earlier, (1_000_000, 1_000_000))
    path = _briefing(
        research / "task-1",
        "Probabilistic Dreaming for World Models https://arxiv.org/abs/2603.04715v1\n"
        "Dexterity from Supervised Play https://arxiv.org/abs/2610.22340\n"
        "Something https://arxiv.org/abs/2610.99999\n",
    )

    assert await _check(path, research) == [
        "2603.04715 was published 2026-03-05, before 2026-10-10",
        "2610.22340 was already in an earlier briefing",
        "arXiv has no paper 2610.99999",
    ]


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        ({}, "the result has no 'file' path"),
        ({"file": "briefing.md"}, "'file' is not an absolute path: briefing.md"),
        ({"file": "/nowhere/2026-10-17.md"}, "the file at /nowhere/2026-10-17.md was not saved, or is empty"),
    ],
)
async def test_a_file_that_was_not_saved_is_caught(output: dict, problem: str) -> None:
    assert await run_checks((CheckSpec("saved_file", {"field": "file"}),), output, today=TODAY) == [problem]


async def test_arxiv_not_answering_does_not_fail_the_briefing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(ids: list[str]) -> dict:
        raise OSError("Rate exceeded.")

    monkeypatch.setattr(checks, "_arxiv_metadata", down)
    path = _briefing(tmp_path / "research" / "t", "Anything https://arxiv.org/abs/2610.22340")

    assert await run_checks((CheckSpec("arxiv_papers", {"field": "file"}),), {"file": str(path)}, today=TODAY) == []


def test_a_skill_can_only_name_checks_north_has() -> None:
    assert parse_checks(["saved_file", {"arxiv_papers": {"days": 7}}]) == (
        CheckSpec("saved_file"),
        CheckSpec("arxiv_papers", {"days": 7}),
    )
    with pytest.raises(ValueError, match="Unknown check 'run_my_script'"):
        parse_checks(["run_my_script"])
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_checks([{"saved_file": "file"}])


def test_adding_checks_does_not_send_a_scheduled_flow_back_to_testing() -> None:
    """A check can only stop a run, so it is not part of what activation fingerprints."""
    contract = {"agent": "general", "success_criteria": ["A briefing was saved."]}
    plain = parse_execution_contract(contract)
    checked = parse_execution_contract({**contract, "checks": ["saved_file"]})

    def fingerprint(execution: SkillExecution) -> str:
        skill = Skill(
            name="brief",
            description="Use when briefing.",
            body="Brief.",
            source=SkillSource.LEARNED,
            directory=Path("/x"),
            execution=execution,
        )
        return skill_fingerprint(skill)

    assert checked.checks == (CheckSpec("saved_file"),)
    assert fingerprint(plain) == fingerprint(checked)


async def test_a_briefing_saved_later_does_not_count_as_sent_before(tmp_path: Path, arxiv) -> None:
    research = tmp_path / "research"
    path = _briefing(research / "task-1", "Dexterity from Supervised Play https://arxiv.org/abs/2610.22340")
    later = _briefing(
        research / "task-2", "Dexterity from Supervised Play https://arxiv.org/abs/2610.22340", "2026-10-18.md"
    )
    os.utime(path, (1_000_000, 1_000_000))
    os.utime(later, (2_000_000, 2_000_000))

    assert await _check(path, research) == []
