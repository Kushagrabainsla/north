"""Tests for the single approval decision point.

These are the precedence table, executable. Read top to bottom they say what
north will and will not do without asking, in each mode - the question that
previously had no single answer because it was decided in seven places.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from approval.approval_memory import ApprovalMemory
from approval.models import Card, CardType
from approval.policy import Action, ActionKind, ApprovalPolicy, Verdict
from approval.unattended import UnattendedPolicy
from config.approval_mode import ApprovalMode

MODES = tuple(ApprovalMode)


def policy(mode: ApprovalMode, *, memory=None, advisor=None) -> ApprovalPolicy:
    return ApprovalPolicy(
        mode_provider=lambda: mode,
        unattended=UnattendedPolicy(),
        approval_memory=memory,
        question_advisor=advisor,
    )


def shell(command: str, **kw) -> Action:
    return Action(agent="bash", kind=ActionKind.SHELL_COMMAND, summary=command, command=command, **kw)


def advisor(decision: str | None, chosen: str = "the learned answer"):
    async def _advise(card: Card) -> tuple[str | None, str]:
        return decision, chosen

    return _advise


def question(*options: str) -> Card:
    return Card.new(type=CardType.QUESTION, agent="general", title="Which?", message="Which?", options=list(options))


# ── 1. Read-only runs in every mode ──────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
async def test_a_read_only_action_never_asks(mode: ApprovalMode) -> None:
    ruling = await policy(mode).rule(shell("ls -la", read_only=True, mutating=False))

    assert ruling.verdict is Verdict.ALLOW
    assert ruling.rule == "read-only"


# ── 2. Autonomous allows everything ──────────────────────────────────────────


async def test_autonomous_allows_a_destructive_command() -> None:
    """The operator declined a hard-danger floor: allow all means allow all."""
    ruling = await policy(ApprovalMode.AUTONOMOUS).rule(shell("rm -rf ~/.north"))

    assert ruling.verdict is Verdict.ALLOW


# ── 3. Work handed over for review is never auto-decided below autonomous ────


@pytest.mark.parametrize("mode", (ApprovalMode.ASK, ApprovalMode.SAFE))
async def test_a_card_carrying_work_always_asks(mode: ApprovalMode) -> None:
    """The regression this whole issue exists for: a job application must not self-submit."""
    submit = Action(
        agent="job",
        kind=ActionKind.OTHER,
        summary="Submit this application?",
        carries_work=True,
    )

    ruling = await policy(mode).rule(submit)

    assert ruling.verdict is Verdict.ASK
    assert ruling.rule == "carries work for review"


async def test_work_is_not_saved_by_a_learned_rule_either(tmp_path: Path) -> None:
    """Every learned tier sits below the carries-work check, not beside it."""
    memory = ApprovalMemory(tmp_path / "m.db")
    submit = Action(agent="job", kind=ActionKind.OTHER, summary="Submit?", carries_work=True)

    ruling = await policy(ApprovalMode.SAFE, memory=memory).rule(submit)

    assert ruling.verdict is Verdict.ASK


async def test_autonomous_still_submits_work() -> None:
    submit = Action(agent="job", kind=ActionKind.OTHER, summary="Submit?", carries_work=True)

    assert (await policy(ApprovalMode.AUTONOMOUS).rule(submit)).verdict is Verdict.ALLOW


# ── 4. The deterministic safe subset, in auto only ───────────────────────────


async def test_auto_allows_an_allowlisted_command() -> None:
    ruling = await policy(ApprovalMode.SAFE).rule(shell("pytest tests/unit"))

    assert ruling.verdict is Verdict.ALLOW
    assert ruling.rule == "auto: safe command allowlist"


async def test_interactive_asks_for_the_same_command() -> None:
    """The middle tier is what `auto` buys; `interactive` must not have it."""
    assert (await policy(ApprovalMode.ASK).rule(shell("pytest tests/unit"))).verdict is Verdict.ASK


async def test_a_chained_command_is_not_in_the_safe_subset() -> None:
    assert (await policy(ApprovalMode.SAFE).rule(shell("pytest && rm -rf /"))).verdict is Verdict.ASK


async def test_auto_allows_a_local_git_action_but_not_a_push() -> None:
    def git(operation: str, args: str = "") -> Action:
        return Action(agent="git", kind=ActionKind.GIT, summary=f"git {operation}", operation=operation, args=args)

    assert (await policy(ApprovalMode.SAFE).rule(git("commit"))).verdict is Verdict.ALLOW
    assert (await policy(ApprovalMode.SAFE).rule(git("push"))).verdict is Verdict.ASK


async def test_auto_allows_an_edit_inside_the_workspace_only(tmp_path: Path) -> None:
    inside = tmp_path / "src" / "app.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("x")

    def edit(path: Path) -> Action:
        return Action(
            agent="patch_file",
            kind=ActionKind.FILE_EDIT,
            summary=f"edit {path}",
            path=path,
            workspace=str(tmp_path),
        )

    assert (await policy(ApprovalMode.SAFE).rule(edit(inside))).verdict is Verdict.ALLOW
    assert (await policy(ApprovalMode.SAFE).rule(edit(Path.home() / ".ssh" / "id_rsa"))).verdict is Verdict.ASK


# ── 5. Learned decisions, in auto only ───────────────────────────────────────


async def test_auto_replays_a_prior_decision(tmp_path: Path) -> None:
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("npm run deploy")
    memory.record(action.agent, action.describe(), "approved")

    ruling = await policy(ApprovalMode.SAFE, memory=memory).rule(action)

    assert ruling.verdict is Verdict.ALLOW
    assert "approved this action before" in ruling.rule


async def test_a_prior_rejection_refuses_rather_than_asking_again(tmp_path: Path) -> None:
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("npm run deploy")
    memory.record(action.agent, action.describe(), "rejected")

    assert (await policy(ApprovalMode.SAFE, memory=memory).rule(action)).verdict is Verdict.REFUSE


async def test_interactive_never_replays_a_prior_decision(tmp_path: Path) -> None:
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("npm run deploy")
    memory.record(action.agent, action.describe(), "approved")

    assert (await policy(ApprovalMode.ASK, memory=memory).rule(action)).verdict is Verdict.ASK


# ── 6. Questions: only YOLO and Autonomous answer for you ──────────────────


async def test_yolo_answers_a_question_yes() -> None:
    assert await policy(ApprovalMode.YOLO).answer(question("Proceed", "Stop")) == ("answered", "Proceed")
    assert (await policy(ApprovalMode.YOLO).answer(question()))[1].startswith("Yes")


async def test_autonomous_lets_the_learned_rules_answer() -> None:
    answered = await policy(ApprovalMode.AUTONOMOUS, advisor=advisor("answered", "Postgres")).answer(question())

    assert answered == ("answered", "Postgres")


@pytest.mark.parametrize("mode", [ApprovalMode.ASK, ApprovalMode.SAFE])
async def test_ask_and_safe_never_let_a_model_answer_for_you(mode: ApprovalMode) -> None:
    """The bug: the judgement tier answered questions in every mode, the strictest included."""
    consulted = False

    async def spy(card: Card) -> tuple[str | None, str]:
        nonlocal consulted
        consulted = True
        return "answered", "x"

    assert await policy(mode, advisor=spy).answer(question()) is None
    assert not consulted


async def test_an_abstaining_advisor_leaves_the_question_to_you() -> None:
    assert await policy(ApprovalMode.AUTONOMOUS, advisor=advisor(None)).answer(question()) is None


async def test_yolo_says_yes_to_every_action() -> None:
    ruling = await policy(ApprovalMode.YOLO).rule(shell("rm -rf build"))

    assert ruling.allowed and ruling.rule.startswith("yolo")


# ── Precedence between tiers ─────────────────────────────────────────────────


async def test_the_safe_subset_wins_over_a_learned_rejection(tmp_path: Path) -> None:
    """A deterministic rule outranks anything learned - hardest evidence first."""
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("pytest tests/unit")
    memory.record(action.agent, action.describe(), "rejected")

    assert (await policy(ApprovalMode.SAFE, memory=memory).rule(action)).verdict is Verdict.ALLOW


# ── Identity is the action, not the prose ────────────────────────────────────


def test_identity_is_built_from_facts_not_the_rendered_message() -> None:
    a = shell("git push --force")
    b = Action(agent="git", kind=ActionKind.GIT, summary="git push --force", operation="push", args="--force")

    assert a.describe() != b.describe()
    assert "cmd=git push --force" in a.describe()
    assert "op=push" in b.describe() and "args=--force" in b.describe()


def test_two_different_commands_have_different_identities() -> None:
    prefix = "cd /very/long/path/that/exceeds/eighty/characters/on/its/own && source .venv/bin/activate && "

    assert shell(prefix + "pytest").describe() != shell(prefix + "rm -rf ~/.north").describe()


# ── Every ruling explains itself ─────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
async def test_every_ruling_names_its_rule(mode: ApprovalMode) -> None:
    """An auto-approval nobody can see is the same as one that never happened."""
    for action in (shell("ls", read_only=True, mutating=False), shell("pytest tests/unit"), shell("rm -rf /")):
        assert (await policy(mode).rule(action)).rule


async def test_autonomous_ignores_a_prior_rejection(tmp_path: Path) -> None:
    """Allow-all sits above every learned tier - replaying is auto's job, not its."""
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("npm run deploy")
    memory.record(action.agent, action.describe(), "rejected")

    assert (await policy(ApprovalMode.AUTONOMOUS, memory=memory).rule(action)).verdict is Verdict.ALLOW


async def test_the_mode_is_read_at_decision_time() -> None:
    """Changing autonomy in settings takes effect on the next action, not the next restart."""
    mode = ApprovalMode.ASK
    live = ApprovalPolicy(mode_provider=lambda: mode, unattended=UnattendedPolicy())
    action = shell("pytest tests/unit")

    assert (await live.rule(action)).verdict is Verdict.ASK

    mode = ApprovalMode.SAFE

    assert (await live.rule(action)).verdict is Verdict.ALLOW


def _edit(path: Path, **kw) -> Action:
    return Action(agent="write_file", kind=ActionKind.FILE_EDIT, summary="write", path=path, **kw)


@pytest.mark.parametrize("mode", MODES)
async def test_north_writing_its_own_notes_never_asks(mode: ApprovalMode, tmp_path: Path) -> None:
    ruling = await policy(mode).rule(_edit(tmp_path / "notes.md", in_north_scratch=True))

    assert ruling.verdict is Verdict.ALLOW


async def test_an_edit_outside_the_granted_folder_asks_in_auto(tmp_path: Path) -> None:
    ruling = await policy(ApprovalMode.SAFE).rule(_edit(tmp_path / "x.md", workspace=str(tmp_path / "task")))

    assert ruling.verdict is Verdict.ASK


async def test_only_file_edits_use_the_scratch_rule(tmp_path: Path) -> None:
    ruling = await policy(ApprovalMode.ASK).rule(shell("rm notes.md", in_north_scratch=True))

    assert ruling.verdict is Verdict.ASK
