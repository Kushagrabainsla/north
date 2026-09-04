"""Withdrawing a learned skill that keeps being present when tasks fail.

north distils skills from its own runs, so it can distil a mistake. One learned
skill instructed every future agent to read a handoff file and stop if it was
missing - the 1.16.0 abort bug, written down as a procedure. Agents followed it,
tasks failed, and nothing ever looked back at the skill.

These cover both halves: refusing to write such a skill in the first place, and
retiring one whose record has gone bad.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from skills import retirement
from skills.models import SkillSource
from skills.registry import SkillRegistry, rejection_reason
from skills.retirement import _with_retired_status, retire, skills_to_retire

_BAD = "task_completed_with_failures"
_GOOD = "task_completed"


def _learned(directory: Path, name: str, *, status: str = "active", body: str = "1. Do the thing.") -> Path:
    path = directory / name
    path.mkdir(parents=True, exist_ok=True)
    frontmatter = yaml.safe_dump(
        {
            "name": name,
            "description": "A learned procedure: read, then decide.",
            "version": "1.0.0",
            "status": status,
            "source": "learned",
            "provenance": ["task_abc"],
        },
        sort_keys=False,
    )
    file = path / "SKILL.md"
    file.write_text(f"---\n{frontmatter}---\n\n{body}\n", encoding="utf-8")
    return file


class TestRefusingToLearnAMachineSpecificProcedure:
    """A procedure naming one machine's absolute paths is a transcript, not a skill."""

    def test_the_real_poisoned_skill_is_rejected(self) -> None:
        body = (
            "1. Locate and read the required task handoff file under "
            "`/Users/somebody/.north/tasks/<task-id>/research/context.md`; if it is missing, stop.\n"
            "2. Write the explanation to /Users/somebody/Desktop/projects/north/summary.md.\n"
        )
        assert "machine-specific path" in rejection_reason("trace-routing", "Trace routing.", body)

    @pytest.mark.parametrize(
        "body",
        [
            "Read /home/dev/project/notes.md first.",
            "Open /root/state/context.md and continue.",
            r"Check C:\Users\dev\project\notes.md before starting.",
        ],
    )
    def test_other_home_rooted_paths_are_rejected_too(self, body: str) -> None:
        assert "machine-specific path" in rejection_reason("s", "d", body)

    @pytest.mark.parametrize(
        "body",
        [
            "1. Read the context file in the handoff directory.",
            "Run `ls ~/.north` to inspect state.",  # portable, stays
            "Open src/cart.py and read apply_discount.",  # workspace-relative, stays
            "The path /usr/bin/git is the binary.",  # not a home directory
        ],
    )
    def test_a_portable_procedure_is_accepted(self, body: str) -> None:
        assert rejection_reason("s", "d", body) == ""

    def test_a_builtin_skill_is_exempt(self) -> None:
        """Hand-authored skills are reviewed by a person; this bar is for what
        north writes about itself."""
        assert rejection_reason("s", "d", "/Users/dev/x.md", source=SkillSource.BUILTIN) == ""

    def test_the_same_bar_refuses_to_load_one_already_on_disk(self, tmp_path) -> None:
        """The rule is shared by the writer and the loader, so a skill distilled
        before the rule existed stops being offered rather than lingering."""
        builtin, learned = tmp_path / "builtin", tmp_path / "learned"
        builtin.mkdir()
        _learned(learned, "poisoned", body="Read /Users/dev/.north/tasks/t1/research/context.md; if missing, stop.")
        _learned(learned, "portable", body="Read the handoff context, then continue.")

        registry = SkillRegistry(builtin, learned_dir=learned)

        assert registry.names() == ["portable"]


class TestDecidingWhatToRetire:
    def test_a_skill_whose_tasks_keep_failing_is_retired(self) -> None:
        selections = [("t1", ["bad"]), ("t2", ["bad"]), ("t3", ["bad"])]
        outcomes = {"t1": _BAD, "t2": _BAD, "t3": _BAD}

        assert skills_to_retire(selections, outcomes) == ["bad"]

    def test_a_skill_that_mostly_works_is_kept(self) -> None:
        selections = [("t1", ["good"]), ("t2", ["good"]), ("t3", ["good"]), ("t4", ["good"])]
        outcomes = {"t1": _GOOD, "t2": _GOOD, "t3": _GOOD, "t4": _BAD}

        assert skills_to_retire(selections, outcomes) == []

    def test_too_few_uses_is_not_a_pattern(self) -> None:
        """Two bad runs out of two is a coincidence. Retiring on it would throw
        away a skill the first time a task went wrong for unrelated reasons."""
        selections = [("t1", ["unlucky"]), ("t2", ["unlucky"])]
        outcomes = {"t1": _BAD, "t2": _BAD}

        assert skills_to_retire(selections, outcomes) == []

    def test_a_task_still_running_is_not_evidence_either_way(self) -> None:
        """Counting an unfinished task as a success would hide a skill that is
        failing right now; as a failure, it would retire one mid-flight."""
        selections = [("t1", ["s"]), ("t2", ["s"]), ("t3", ["s"]), ("t4", ["s"])]
        outcomes = {"t1": _BAD, "t2": _BAD}  # t3, t4 have not finished

        assert skills_to_retire(selections, outcomes) == []

    def test_each_skill_is_tallied_on_its_own_record(self) -> None:
        """Two skills used on the same run must not share its verdict."""
        selections = [
            ("t1", ["bad", "innocent"]),
            ("t2", ["bad"]),
            ("t3", ["bad"]),
            ("t4", ["innocent"]),
            ("t5", ["innocent"]),
        ]
        outcomes = {"t1": _BAD, "t2": _BAD, "t3": _BAD, "t4": _GOOD, "t5": _GOOD}

        # `innocent` was there for one failure and two successes: 1 of 3, under
        # the ratio. `bad` was there for three of three.
        assert skills_to_retire(selections, outcomes) == ["bad"]

    def test_no_data_retires_nothing(self) -> None:
        assert skills_to_retire([], {}) == []


class TestRetiringOnDisk:
    def test_status_becomes_retired_and_the_record_survives(self, tmp_path) -> None:
        """The body and provenance stay: what north believed, and that it was
        withdrawn, is the useful part."""
        file = _learned(tmp_path, "bad", body="1. Stop if the file is missing.")

        assert retire(tmp_path, ["bad"]) == ["bad"]

        document = file.read_text()
        frontmatter = yaml.safe_load(document.split("---", 2)[1])
        assert frontmatter["status"] == "retired"
        assert frontmatter["provenance"] == ["task_abc"]
        assert "1. Stop if the file is missing." in document

    def test_a_retired_skill_is_no_longer_offered(self, tmp_path) -> None:
        builtin, learned = tmp_path / "builtin", tmp_path / "learned"
        builtin.mkdir()
        _learned(learned, "bad")
        retire(learned, ["bad"])

        registry = SkillRegistry(builtin, learned_dir=learned)

        assert not registry.get("bad").available_to("engineering"), "a retired skill must not be selected"

    def test_a_hand_authored_skill_is_never_touched(self, tmp_path) -> None:
        """Evidence about the tasks, not about a skill a person wrote."""
        path = tmp_path / "human"
        path.mkdir()
        document = "---\nname: human\ndescription: d\nstatus: active\nsource: builtin\n---\n\nBody.\n"
        (path / "SKILL.md").write_text(document, encoding="utf-8")

        assert retire(tmp_path, ["human"]) == []
        assert (path / "SKILL.md").read_text() == document

    def test_retiring_twice_changes_nothing(self, tmp_path) -> None:
        _learned(tmp_path, "bad")
        assert retire(tmp_path, ["bad"]) == ["bad"]
        assert retire(tmp_path, ["bad"]) == [], "already retired is not a change"

    def test_a_missing_skill_is_skipped_quietly(self, tmp_path) -> None:
        """This runs on a schedule; it must never be why a cleanup pass fails."""
        assert retire(tmp_path, ["not-there"]) == []

    @pytest.mark.parametrize(
        "document",
        ["no frontmatter at all", "---\nname: x\n", "---\n\tnot: [valid yaml\n---\nbody"],
        ids=["no frontmatter", "unterminated", "invalid yaml"],
    )
    def test_a_malformed_file_is_left_alone(self, document: str) -> None:
        assert _with_retired_status(document) is None

    def test_a_description_with_yaml_punctuation_survives(self, tmp_path) -> None:
        """Patching the status by string surgery would corrupt frontmatter whose
        description contains ': ' or '#' - and an unparseable skill silently
        stops loading."""
        path = tmp_path / "tricky"
        path.mkdir()
        frontmatter = yaml.safe_dump(
            {
                "name": "tricky",
                "description": "Use when: the build fails # especially on CI",
                "status": "active",
                "source": "learned",
            },
            sort_keys=False,
        )
        (path / "SKILL.md").write_text(f"---\n{frontmatter}---\n\nBody.\n", encoding="utf-8")

        assert retire(tmp_path, ["tricky"]) == ["tricky"]

        reloaded = yaml.safe_load((path / "SKILL.md").read_text().split("---", 2)[1])
        assert reloaded["description"] == "Use when: the build fails # especially on CI"
        assert reloaded["status"] == "retired"


class TestTheSweepOverARealLedger:
    """The pure policy is tested above; this is the wiring around it."""

    async def _ledger_with(self, tmp_path, rows: list[tuple[str, str, str]]):
        """rows: (task_id, action, output)."""
        from ledger import SQLiteLedgerWriter
        from ledger.models import LedgerEntry, LedgerSource, LedgerStatus

        ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
        for task_id, action, output in rows:
            await ledger.write(
                LedgerEntry.new(
                    source=LedgerSource.SYSTEM,
                    task_id=task_id,
                    action=action,
                    output=output,
                    status=LedgerStatus.COMPLETED,
                )
            )
        return ledger

    async def test_a_skill_present_in_failing_tasks_is_retired_end_to_end(self, tmp_path) -> None:
        learned = tmp_path / "learned"
        _learned(learned, "bad")
        ledger = await self._ledger_with(
            tmp_path,
            [
                ("t1", "skill_selected", "bad"),
                ("t1", "task_completed_with_failures", ""),
                ("t2", "skill_selected", "bad"),
                ("t2", "task_failed", ""),
                ("t3", "skill_selected", "bad"),
                ("t3", "task_completed_with_failures", ""),
            ],
        )

        assert await retirement.sweep(ledger, learned) == ["bad"]

    async def test_a_skill_present_in_healthy_tasks_is_left_alone(self, tmp_path) -> None:
        learned = tmp_path / "learned"
        _learned(learned, "good")
        ledger = await self._ledger_with(
            tmp_path,
            [
                ("t1", "skill_selected", "good"),
                ("t1", "task_completed", ""),
                ("t2", "skill_selected", "good"),
                ("t2", "task_completed", ""),
                ("t3", "skill_selected", "good"),
                ("t3", "task_completed", ""),
            ],
        )

        assert await retirement.sweep(ledger, learned) == []

    async def test_the_comma_separated_selection_output_is_split(self, tmp_path) -> None:
        """`skill_selected` records several names in one row, as the agent
        writes them - a sweep that read the row whole would tally nothing."""
        learned = tmp_path / "learned"
        _learned(learned, "alpha")
        _learned(learned, "beta")
        ledger = await self._ledger_with(
            tmp_path,
            [
                ("t1", "skill_selected", "alpha, beta"),
                ("t1", "task_failed", ""),
                ("t2", "skill_selected", "alpha, beta"),
                ("t2", "task_failed", ""),
                ("t3", "skill_selected", "alpha, beta"),
                ("t3", "task_failed", ""),
            ],
        )

        assert await retirement.sweep(ledger, learned) == ["alpha", "beta"]

    async def test_a_broken_ledger_does_not_take_the_cleanup_pass_down(self, tmp_path) -> None:
        class _Exploding:
            async def query(self, _filters):
                raise RuntimeError("ledger unavailable")

        assert await retirement.sweep(_Exploding(), tmp_path / "learned") == []
