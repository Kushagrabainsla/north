"""The single place that decides whether north acts without asking.

Before this module the decision lived in seven places: `is_instantly_safe` and
an allowlist check inside BashTool, an allowlist check inside GitTool, another
inside PatchFileTool, and three more in JudgementFilter (autonomous approves
everything, auto replays a learned decision, an LLM answers at high confidence).
Nothing could answer "what will north do without asking me?", and the LLM tier
had no mode check at all - so it auto-approved in `interactive`, the default.

The split is now: **a tool describes what it is about to do; this module rules
on it.** Tools keep the knowledge that is genuinely theirs - bash knows whether
a command chains or redirects, git knows whether an action mutates, patch_file
knows whether a path is inside the workspace - and express it as facts on an
`Action`. They no longer read the approval mode or reach for a policy.

Every ruling names the rule that produced it, so an action that ran without
asking can still be shown afterwards. An auto-approval nobody can see is
indistinguishable from one that never happened.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from approval.models import ApprovalDecision, DecidedBy, MemoryRef
from approval.unattended import forbidden_reason
from config.approval_mode import YES_ANSWER, ApprovalMode, approve_option

if TYPE_CHECKING:
    from approval.approval_memory import ApprovalMemory
    from approval.models import Card
    from approval.unattended import UnattendedPolicy

logger = logging.getLogger(__name__)


class ActionKind(StrEnum):
    """What sort of thing a tool is about to do.

    The policy rules on facts, not on the agent's name: a hardcoded list of
    names only protects what somebody remembered to add to it, which is how
    every new agent started out unprotected.
    """

    SHELL_COMMAND = "shell_command"
    FILE_EDIT = "file_edit"
    GIT = "git"
    GITHUB = "github"
    DEVICE = "device"
    BROWSER = "browser"
    MCP = "mcp"
    TOOL_CHANGE = "tool_change"
    OTHER = "other"


class Verdict(StrEnum):
    ALLOW = "allow"  # run it, no card
    ASK = "ask"  # surface a card and wait
    REFUSE = "refuse"  # never, whatever the mode


@dataclass(frozen=True)
class Action:
    """What a tool is about to do, described rather than judged.

    A tool fills in the facts that apply to it and leaves the rest. `summary`
    is for the human on the card; every other field is what the policy reads,
    so a decision is never taken by matching against formatted prose.
    """

    agent: str
    kind: ActionKind
    summary: str

    # Facts, filled in by whichever tool they belong to.
    command: str = ""
    operation: str = ""  # git/gh/device action name
    args: str = ""
    path: Path | None = None
    workspace: str = ""

    # The tool's own classification of itself.
    mutating: bool = True
    # The tool has *proved* this touches nothing - a read-only shell command, a
    # git status. Not a guess, and not "probably safe".
    read_only: bool = False
    # This action is work north filled in and is handing over for a decision
    # (an approval card with fields). Never auto-decided below autonomous: the
    # fields exist precisely because a human is meant to look at them.
    carries_work: bool = False
    # The tool recognises this as one of a few catastrophic shapes (`rm -rf /`,
    # a fork bomb, writing to a raw device). Refused outright below autonomous,
    # where the operator has made the mode the only authority.
    obviously_destructive: bool = False
    # Sends something to a person other than the operator, or spends money.
    # Never auto-approved below autonomous, whatever the rule table says: a sent
    # email cannot be unsent, and a payment cannot be taken back. Tools that know
    # set these; `forbidden_reason` has a word-list backstop for those that
    # do not yet.
    reaches_third_party: bool = False
    spends_money: bool = False
    # Reaches past the sandbox and the workspace: a push, a host the agent was not
    # given. In autonomous the memory decider approves these only when something
    # the user stated covers them - with nothing, a card waits (approval/decider.py).
    leaves_sandbox: bool = False
    # A file edit inside north's own scratch space (task handoff notes, personal
    # notes) rather than anywhere of the user's. See `tools._path.is_north_scratch`.
    in_north_scratch: bool = False
    # What a person would read to judge the action - a card's filled-in fields.
    # Shown to the memory decider; not part of the action's identity.
    details: str = ""

    def describe(self) -> str:
        """A short, stable identity for this action, for learned decisions.

        Built from the facts rather than the rendered message, so "did you
        approve this before?" asks about the action itself and not about a
        sentence that happened to contain it.
        """
        parts = [self.agent, self.kind.value]
        for name, value in (
            ("op", self.operation),
            ("cmd", self.command),
            ("args", self.args),
            ("path", str(self.path) if self.path else ""),
        ):
            if value:
                parts.append(f"{name}={value}")
        return " ".join(parts)


@dataclass(frozen=True)
class Ruling:
    """A verdict plus the rule that produced it, and the memory it used, if any."""

    verdict: Verdict
    rule: str
    decided_by: str = ""
    memory_used: tuple[MemoryRef, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


@dataclass(frozen=True)
class Answer:
    """A card decided without the user: the decision, the option taken, and why."""

    decision: str
    chosen_option: str
    reason: str
    decided_by: str
    memory_used: tuple[MemoryRef, ...] = ()


class Decider(Protocol):
    """Decides for the user in Autonomous, from what north knows about them.

    Returns None when it cannot decide - no model, an error, an unreadable
    reply - and the card then waits for the user. See `approval/decider.py`.
    """

    async def rule(self, action: Action) -> Answer | None: ...

    async def answer(self, card: Card) -> Answer | None: ...


@dataclass
class ApprovalPolicy:
    """Rules on an `Action`, and on who answers a question. The only code that reads the approval mode."""

    mode_provider: Callable[[], ApprovalMode] = lambda: ApprovalMode.ASK
    unattended: UnattendedPolicy | None = None
    approval_memory: ApprovalMemory | None = None
    decider: Decider | None = None

    async def rule(self, action: Action) -> Ruling:
        """Decide whether *action* runs, asks, or is refused.

        The tiers are ordered from the least to the most judgement involved -
        a fact about the action first, the mode next, a deterministic allowlist
        after that, and only then anything learned. The first that matches wins,
        so a later, softer tier can never overturn an earlier, harder one.
        """
        mode = self.mode_provider()

        # 1. Reads nothing and changes nothing. True in every mode - this is what
        #    "ask" already means by "read-only actions run freely".
        if action.read_only or not action.mutating:
            return Ruling(Verdict.ALLOW, "read-only", DecidedBy.NORTH)

        # 1b. North writing its own notes. Asking about these would stall every
        #     unattended run that writes a handoff or a briefing, in every mode.
        if action.kind is ActionKind.FILE_EDIT and action.in_north_scratch:
            return Ruling(Verdict.ALLOW, "north's own scratch space", DecidedBy.NORTH)

        # 2. Modes that never ask. The operator has explicitly declined a
        #    hard-danger floor in both.
        if mode is ApprovalMode.YOLO:
            return Ruling(Verdict.ALLOW, "yes to everything", DecidedBy.YOLO)
        if mode is ApprovalMode.AUTONOMOUS:
            return await self._autonomous(action)

        # 3. A handful of catastrophic shapes are refused rather than asked
        #    about, below autonomous. Not a floor - autonomous passed above.
        if action.obviously_destructive:
            return Ruling(Verdict.REFUSE, "recognised as catastrophic", DecidedBy.NORTH)

        # 4. Work handed over for review is never auto-decided below autonomous.
        #    Checked before every learned tier: the fields are the thing a human
        #    is meant to read, so nothing may answer on their behalf.
        if action.carries_work:
            return Ruling(Verdict.ASK, "carries work for review")

        if mode is ApprovalMode.SAFE:
            # 5. The deterministic safe subset: workspace-scoped edits, an
            #    allowlist of test/lint/build commands, local-only git. No model
            #    involved, which is why it can be trusted without a human.
            if (safe := self._safe_subset(action)) is not None:
                return safe

            # 6. The user's own prior decision for this exact action. Safe has
            #    no model tier: only deterministic facts and your own answers.
            if (recalled := self._recall(action)) is not None:
                return recalled

        return Ruling(Verdict.ASK, "not covered by any rule")

    async def answer(self, card: Card) -> Answer | None:
        """Answer a question card for the user, or None to ask.

        YOLO gives the yes answer. Autonomous lets the memory decider answer.
        Ask and Safe put every question to you: no model answers in your place.
        """
        mode = self.mode_provider()
        if mode is ApprovalMode.YOLO:
            chosen = approve_option(card.options) if card.options else YES_ANSWER
            return Answer(ApprovalDecision.ANSWERED, chosen, "yes to everything", DecidedBy.YOLO)
        if mode is ApprovalMode.AUTONOMOUS and self.decider is not None:
            return await self.decider.answer(card)
        return None

    async def _autonomous(self, action: Action) -> Ruling:
        """Autonomous never asks: the deterministic tiers first, then the memory decider.

        Prepared work skips the safe list and replays: its fields are what is
        being judged, and neither tier reads them. Only a decider that cannot
        decide leaves a card for the user.
        """
        if not action.carries_work:
            if (safe := self._safe_subset(action)) is not None:
                return safe
            if (recalled := self._recall(action)) is not None:
                return recalled
        if self.decider is not None and (answer := await self.decider.rule(action)) is not None:
            verdict = Verdict.ALLOW if answer.decision == ApprovalDecision.APPROVED else Verdict.REFUSE
            return Ruling(verdict, answer.reason, DecidedBy.MEMORY_DECIDER, answer.memory_used)
        return Ruling(Verdict.ASK, "the memory decider could not decide")

    def _safe_subset(self, action: Action) -> Ruling | None:
        """The deterministic allowlist, or None when it does not apply.

        The hard rules are checked first, so no row in the rule table - and no
        entry someone adds on the web UI - can make a send or a spend
        auto-approvable. The table decides what is safe; it does not get to
        decide what is unsafe.
        """
        if self.unattended is None:
            return None
        if forbidden_reason(action):
            return None
        if action.kind is ActionKind.SHELL_COMMAND and self.unattended.approves_command(action.command):
            return Ruling(Verdict.ALLOW, "test, lint or build command", DecidedBy.SAFE_LIST)
        if action.kind is ActionKind.GIT and self.unattended.approves_git(action.operation, action.args):
            return Ruling(Verdict.ALLOW, "local-only git", DecidedBy.SAFE_LIST)
        if action.kind is ActionKind.DEVICE and self.unattended.approves_device(action.operation):
            return Ruling(Verdict.ALLOW, "reversible device toggle", DecidedBy.SAFE_LIST)
        if self.unattended.approves_self_message(action.operation):
            return Ruling(Verdict.ALLOW, "message to you", DecidedBy.SAFE_LIST)
        if (
            action.kind is ActionKind.FILE_EDIT
            and action.path is not None
            and self.unattended.approves_edit(action.path, action.workspace or None)
        ):
            return Ruling(Verdict.ALLOW, "edit inside the task workspace", DecidedBy.SAFE_LIST)
        return None

    def _recall(self, action: Action) -> Ruling | None:
        """Replay the user's own prior decision for this exact action."""
        if self.approval_memory is None:
            return None
        recalled = self.approval_memory.recall(action.agent, action.describe())
        if recalled == "approved":
            return Ruling(Verdict.ALLOW, "you approved this action before", DecidedBy.REPLAY)
        if recalled == "rejected":
            return Ruling(Verdict.REFUSE, "you rejected this action before", DecidedBy.REPLAY)
        return None
