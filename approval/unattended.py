"""The deterministic safe-action subset - the `auto` tier.

Pure logic: whether a *given* action is in the safe subset. Whether that subset
is auto-approved is decided by the caller from the live approval mode, so a mode
change takes effect immediately without rebuilding this policy.

The list of what counts as safe is no longer written here. It lives in
`approval/unattended_rules.py`, in the database, and you edit it on the web UI -
see that module for why. What stays here is the matching logic, which is not
yours to configure, and the two hard rules, which are not yours to turn off.

## The two hard rules

**Sending anything to another human is never auto-approvable.**
**Spending money is never auto-approvable.**

They are checked after the rule table, so a row saying otherwise cannot take
effect. That asymmetry is deliberate: as rows they would be one careless write
away from being off, and the failure is silent and one-way.

The reason they exist at all is that the engineering list is defensible because
software actions are reversible - a bad `pytest` costs seconds, a bad edit is
`git revert`. Life actions frequently have no undo. A sent email cannot be
unsent; a booked ticket, a submitted form, a payment are all one-way. So this
list stays conservative rather than being extended by analogy from the coding
one.

Nothing here needs a model. `JudgementFilter` is non-deterministic and is
already forbidden from these paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from approval.unattended_rules import (
    KIND_COMMAND,
    KIND_DEVICE,
    KIND_GIT,
    KIND_SELF_MESSAGE,
    UnattendedRuleStore,
)
from tools._path import is_sensitive_path

if TYPE_CHECKING:
    from approval.policy import Action

# Any of these makes a command unsafe to auto-run: command chaining, command
# substitution, subshells, pipes, or writing redirects. A benign trailing
# ``2>&1`` (merge stderr into stdout) is stripped before this check.
_DANGEROUS = re.compile(r"[;&|`$<>(){}\n]")
_TRAILING_STDERR_REDIRECT = re.compile(r"\s+2>&1\s*$")

# Even for a safe git action, these argument flags are destructive.
_GIT_DANGEROUS_ARGS: frozenset[str] = frozenset({"-f", "--force", "-D", "--delete", "--hard"})

# Backstop for the two hard rules, for tools that have not yet declared
# themselves via `Action.reaches_third_party` / `Action.spends_money`. The flags
# are authoritative; this catches what has not been migrated.
#
# A denylist is the wrong instrument for deciding what north *may* do - that is
# the argument in #18 - but it is the right one here, because the only thing it
# can do is force a card. A false positive costs one question; a false negative
# sends an email in your name.
_OUTBOUND_WORDS: frozenset[str] = frozenset(
    {
        "email",
        "sendmail",
        "smtp",
        "sms",
        "whatsapp",
        "slack",
        "tweet",
        "post",
        "publish",
        "reply",
        "dm",
        "invite",
        "share",
        "submit",
    }
)
_SPENDING_WORDS: frozenset[str] = frozenset(
    {
        "pay",
        "payment",
        "purchase",
        "buy",
        "checkout",
        "charge",
        "invoice",
        "transfer",
        "withdraw",
        "order",
        "book",
        "subscribe",
        "stripe",
        "paypal",
    }
)
_WORD = re.compile(r"[a-z0-9_]+")


def _mentions(text: str, words: frozenset[str]) -> bool:
    return bool(words & set(_WORD.findall(text.lower())))


def forbidden_reason(action: Action) -> str:
    """Why *action* may never be auto-approved, or "" when nothing forbids it.

    Consulted after the rule table. The flags on the action are authoritative;
    the word lists are a backstop for tools that do not set them yet.
    """
    if getattr(action, "reaches_third_party", False):
        return "sending something to another person is never auto-approved"
    if getattr(action, "spends_money", False):
        return "spending money is never auto-approved"
    haystack = f"{action.operation} {action.command} {action.summary}"
    if _mentions(haystack, _OUTBOUND_WORDS):
        return "this looks like it sends something to another person"
    if _mentions(haystack, _SPENDING_WORDS):
        return "this looks like it spends money"
    return ""


@dataclass(frozen=True)
class UnattendedPolicy:
    """Deterministic predicates for the SAFE action subset (the `auto` tier).

    Backed by `UnattendedRuleStore`, so what counts as safe is what the rule
    table says right now - editing a rule on the web UI takes effect on the next
    action, with no restart.

    Without a store this falls back to the shipped defaults, so a caller that
    has no database (a test, an offline tool) still gets north's behaviour
    rather than an empty allowlist that silently approves nothing.
    """

    store: UnattendedRuleStore | None = None
    # Extra commands from settings, kept for configs that predate the rule table.
    extra_commands: tuple[str, ...] = field(default_factory=tuple)

    def _patterns(self, kind: str) -> tuple[str, ...]:
        if self.store is not None:
            return self.store.patterns(kind)
        return UnattendedRuleStore.shipped_patterns(kind)

    def _fired(self, kind: str, pattern: str) -> None:
        if self.store is not None:
            self.store.record_fire(kind, pattern)

    def approves_edit(self, resolved_path: Path, workspace: str | None) -> bool:
        """True when an edit to *resolved_path* is inside *workspace* and not sensitive.

        Not a rule row: "inside the workspace this task was given" is a relation
        between two paths, not a pattern anyone could usefully write down.
        """
        if not workspace:
            return False
        if is_sensitive_path(resolved_path):
            return False
        try:
            resolved_path.resolve().relative_to(Path(workspace).resolve())
        except (ValueError, OSError):
            return False
        return True

    def approves_command(self, command: str) -> bool:
        """True when *command* matches an enabled command rule, with no chaining."""
        cleaned = _TRAILING_STDERR_REDIRECT.sub("", command.strip())
        if not cleaned or _DANGEROUS.search(cleaned):
            return False
        for pattern in (*self._patterns(KIND_COMMAND), *self.extra_commands):
            if cleaned == pattern or cleaned.startswith(pattern + " "):
                self._fired(KIND_COMMAND, pattern)
                return True
        return False

    def approves_git(self, action: str, args: str = "") -> bool:
        """True for an enabled, local git action (never push/pull/merge/force)."""
        name = action.strip()
        if name not in self._patterns(KIND_GIT):
            return False
        if any(tok in _GIT_DANGEROUS_ARGS for tok in args.split()):
            return False
        self._fired(KIND_GIT, name)
        return True

    def approves_device(self, operation: str) -> bool:
        """True for a trivially reversible device verb - the undo is another toggle.

        Verb-by-verb, never a blanket "smart home" category: "unlock" is a toggle
        by shape and must not be reachable by describing it as one.
        """
        verb = operation.strip().lower()
        if verb not in self._patterns(KIND_DEVICE):
            return False
        self._fired(KIND_DEVICE, verb)
        return True

    def approves_self_message(self, operation: str) -> bool:
        """True for a message addressed to the operator themselves.

        A digest, a reminder, an alert. The recipient is resolved from north's
        own configuration by the tool, never from an address the task supplied -
        anything else is a message to another human, which `forbidden_reason`
        refuses regardless of what this returns.
        """
        name = operation.strip()
        if name not in self._patterns(KIND_SELF_MESSAGE):
            return False
        self._fired(KIND_SELF_MESSAGE, name)
        return True

    @classmethod
    def from_settings(cls, settings: object, store: UnattendedRuleStore | None = None) -> UnattendedPolicy:
        extra = tuple(getattr(settings, "unattended_extra_commands", ()) or ())
        return cls(store=store, extra_commands=extra)
