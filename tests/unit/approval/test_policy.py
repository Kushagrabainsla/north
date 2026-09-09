"""Tests for the single approval decision point.

These are the precedence table, executable. Read top to bottom they say what
north will and will not do without asking, in each mode - the question that
previously had no single answer because it was decided in seven places.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from approval.approval_memory import ApprovalMemory
from approval.mode import ApprovalMode
from approval.policy import Action, ActionKind, ApprovalPolicy, Verdict
from approval.unattended import UnattendedPolicy

MODES = (ApprovalMode.INTERACTIVE, ApprovalMode.AUTO, ApprovalMode.AUTONOMOUS)


def policy(mode: ApprovalMode, *, memory=None, advisor=None) -> ApprovalPolicy:
    return ApprovalPolicy(
        mode_provider=lambda: mode,
        unattended=UnattendedPolicy(),
        approval_memory=memory,
        llm_advisor=advisor,
    )


def shell(command: str, **kw) -> Action:
    return Action(agent="bash", kind=ActionKind.SHELL_COMMAND, summary=command, command=command, **kw)


def advisor(decision: str | None, reason: str = "a learned rule"):
    async def _advise(action: Action) -> tuple[str | None, str]:
        return decision, reason

    return _advise


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


@pytest.mark.parametrize("mode", (ApprovalMode.INTERACTIVE, ApprovalMode.AUTO))
async def test_a_card_carrying_work_always_asks(mode: ApprovalMode) -> None:
    """The regression this whole issue exists for: a job application must not self-submit."""
    submit = Action(
        agent="job",
        kind=ActionKind.OTHER,
        summary="Submit this application?",
        carries_work=True,
    )

    ruling = await policy(mode, advisor=advisor("approved")).rule(submit)

    assert ruling.verdict is Verdict.ASK
    assert ruling.rule == "carries work for review"


async def test_work_is_not_saved_by_a_learned_rule_either(tmp_path: Path) -> None:
    """Every learned tier sits below the carries-work check, not beside it."""
    memory = ApprovalMemory(tmp_path / "m.db")
    submit = Action(agent="job", kind=ActionKind.OTHER, summary="Submit?", carries_work=True)

    ruling = await policy(ApprovalMode.AUTO, memory=memory, advisor=advisor("approved")).rule(submit)

    assert ruling.verdict is Verdict.ASK


async def test_autonomous_still_submits_work() -> None:
    submit = Action(agent="job", kind=ActionKind.OTHER, summary="Submit?", carries_work=True)

    assert (await policy(ApprovalMode.AUTONOMOUS).rule(submit)).verdict is Verdict.ALLOW


# ── 4. The deterministic safe subset, in auto only ───────────────────────────


async def test_auto_allows_an_allowlisted_command() -> None:
    ruling = await policy(ApprovalMode.AUTO).rule(shell("pytest tests/unit"))

    assert ruling.verdict is Verdict.ALLOW
    assert ruling.rule == "auto: safe command allowlist"


async def test_interactive_asks_for_the_same_command() -> None:
    """The middle tier is what `auto` buys; `interactive` must not have it."""
    assert (await policy(ApprovalMode.INTERACTIVE).rule(shell("pytest tests/unit"))).verdict is Verdict.ASK


async def test_a_chained_command_is_not_in_the_safe_subset() -> None:
    assert (await policy(ApprovalMode.AUTO).rule(shell("pytest && rm -rf /"))).verdict is Verdict.ASK


async def test_auto_allows_a_local_git_action_but_not_a_push() -> None:
    def git(operation: str, args: str = "") -> Action:
        return Action(agent="git", kind=ActionKind.GIT, summary=f"git {operation}", operation=operation, args=args)

    assert (await policy(ApprovalMode.AUTO).rule(git("commit"))).verdict is Verdict.ALLOW
    assert (await policy(ApprovalMode.AUTO).rule(git("push"))).verdict is Verdict.ASK


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

    assert (await policy(ApprovalMode.AUTO).rule(edit(inside))).verdict is Verdict.ALLOW
    assert (await policy(ApprovalMode.AUTO).rule(edit(Path.home() / ".ssh" / "id_rsa"))).verdict is Verdict.ASK


# ── 5. Learned decisions, in auto only ───────────────────────────────────────


async def test_auto_replays_a_prior_decision(tmp_path: Path) -> None:
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("npm run deploy")
    memory.record(action.agent, action.describe(), "approved")

    ruling = await policy(ApprovalMode.AUTO, memory=memory).rule(action)

    assert ruling.verdict is Verdict.ALLOW
    assert "approved this action before" in ruling.rule


async def test_a_prior_rejection_refuses_rather_than_asking_again(tmp_path: Path) -> None:
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("npm run deploy")
    memory.record(action.agent, action.describe(), "rejected")

    assert (await policy(ApprovalMode.AUTO, memory=memory).rule(action)).verdict is Verdict.REFUSE


async def test_interactive_never_replays_a_prior_decision(tmp_path: Path) -> None:
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("npm run deploy")
    memory.record(action.agent, action.describe(), "approved")

    assert (await policy(ApprovalMode.INTERACTIVE, memory=memory).rule(action)).verdict is Verdict.ASK


# ── 6. The learned judgement rules, in auto only ─────────────────────────────


async def test_the_advisor_can_decide_in_auto() -> None:
    ruling = await policy(ApprovalMode.AUTO, advisor=advisor("approved", "user always deploys on green")).rule(
        shell("npm run deploy")
    )

    assert ruling.verdict is Verdict.ALLOW
    assert "user always deploys on green" in ruling.rule


async def test_the_advisor_is_never_consulted_in_interactive() -> None:
    """The bug: this tier had no mode check, so it decided in the default mode."""
    consulted = False

    async def spy(action: Action) -> tuple[str | None, str]:
        nonlocal consulted
        consulted = True
        return "approved", ""

    ruling = await policy(ApprovalMode.INTERACTIVE, advisor=spy).rule(shell("npm run deploy"))

    assert ruling.verdict is Verdict.ASK
    assert not consulted, "interactive must not ask a model whether to skip asking the human"


async def test_an_advisor_failure_falls_through_to_asking() -> None:
    async def broken(action: Action) -> tuple[str | None, str]:
        raise RuntimeError("model unavailable")

    assert (await policy(ApprovalMode.AUTO, advisor=broken).rule(shell("npm run deploy"))).verdict is Verdict.ASK


async def test_an_abstaining_advisor_falls_through_to_asking() -> None:
    assert (await policy(ApprovalMode.AUTO, advisor=advisor(None)).rule(shell("npm run deploy"))).verdict is Verdict.ASK


# ── Precedence between tiers ─────────────────────────────────────────────────


async def test_the_safe_subset_wins_over_a_learned_rejection(tmp_path: Path) -> None:
    """A deterministic rule outranks anything learned - hardest evidence first."""
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("pytest tests/unit")
    memory.record(action.agent, action.describe(), "rejected")

    assert (await policy(ApprovalMode.AUTO, memory=memory).rule(action)).verdict is Verdict.ALLOW


async def test_a_recalled_decision_wins_over_the_advisor(tmp_path: Path) -> None:
    """The user's own verdict outranks a model's reading of their rules."""
    memory = ApprovalMemory(tmp_path / "m.db")
    action = shell("npm run deploy")
    memory.record(action.agent, action.describe(), "rejected")

    ruling = await policy(ApprovalMode.AUTO, memory=memory, advisor=advisor("approved")).rule(action)

    assert ruling.verdict is Verdict.REFUSE


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
    mode = ApprovalMode.INTERACTIVE
    live = ApprovalPolicy(mode_provider=lambda: mode, unattended=UnattendedPolicy())
    action = shell("pytest tests/unit")

    assert (await live.rule(action)).verdict is Verdict.ASK

    mode = ApprovalMode.AUTO

    assert (await live.rule(action)).verdict is Verdict.ALLOW
