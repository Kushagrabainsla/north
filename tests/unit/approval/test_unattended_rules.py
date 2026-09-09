"""The safe-action list as data you own, and the two things you cannot own.

Issue #4. The list used to be a tuple in a source file: entirely
engineering-shaped, and not yours to change - you could not see the effective
list, add to it, or take out an entry you disagreed with. `approval_memory` next
door already states the principle these inherit: a decision that cannot be
withdrawn is not consent.

The load-bearing tests are at the bottom. Everything above is CRUD; those are
the two rules that no row in the table may override, however it got there.
"""

from __future__ import annotations

import pytest

from approval.mode import ApprovalMode
from approval.policy import Action, ActionKind, ApprovalPolicy, Verdict
from approval.unattended import UnattendedPolicy, forbidden_reason
from approval.unattended_rules import UnattendedRuleStore, rule_id


@pytest.fixture
def store(tmp_path) -> UnattendedRuleStore:
    return UnattendedRuleStore(tmp_path / "approval_memory.db")


# ── Seeding ──────────────────────────────────────────────────────────────────


def test_shipped_rules_are_seeded_as_editable_rows(store) -> None:
    rules = store.all()
    assert rules, "north must ship with a working safe-action list"
    assert all(r.source == "builtin" for r in rules)
    assert "pytest" in store.patterns("command")


def test_a_disabled_shipped_rule_stays_disabled_across_restarts(tmp_path) -> None:
    """An upgrade must not quietly bring back a rule you turned off."""
    db = tmp_path / "approval_memory.db"
    first = UnattendedRuleStore(db)
    first.delete(rule_id("command", "pytest"))
    assert "pytest" not in first.patterns("command")

    second = UnattendedRuleStore(db)  # re-seeds on construction

    assert "pytest" not in second.patterns("command")


def test_a_rule_shipped_later_still_appears(tmp_path, monkeypatch) -> None:
    """Tracked per rule, not by one 'seeded' flag.

    A single flag means a rule added in a later version never appears for anyone
    who already ran the old one - a default that silently does not apply.
    """
    import approval.unattended_rules as mod

    db = tmp_path / "approval_memory.db"
    UnattendedRuleStore(db)

    monkeypatch.setattr(mod, "_BUILTINS", (("command", ("pytest", "bazel test")),))
    upgraded = UnattendedRuleStore(db)

    assert "bazel test" in upgraded.patterns("command")


def test_restoring_brings_back_what_was_removed(store) -> None:
    store.delete(rule_id("command", "pytest"))
    assert store.restore_builtins() >= 1
    assert "pytest" in store.patterns("command")


# ── CRUD ─────────────────────────────────────────────────────────────────────


def test_a_rule_you_add_takes_effect_without_a_restart(store) -> None:
    policy = UnattendedPolicy(store=store)
    assert policy.approves_command("make lint") is False

    store.add("command", "make lint")

    assert policy.approves_command("make lint") is True, "the same policy object must see the new rule"


def test_a_disabled_rule_stops_approving(store) -> None:
    policy = UnattendedPolicy(store=store)
    assert policy.approves_command("pytest") is True

    store.update(rule_id("command", "pytest"), enabled=False)

    assert policy.approves_command("pytest") is False


def test_a_user_rule_is_deleted_but_a_shipped_one_is_disabled(store) -> None:
    store.add("command", "make lint")
    store.delete(rule_id("command", "make lint"))
    store.delete(rule_id("command", "pytest"))

    ids = {r.id for r in store.all()}
    assert rule_id("command", "make lint") not in ids, "your own rule is simply gone"
    assert rule_id("command", "pytest") in ids, "a shipped rule stays listed so it can be restored"


def test_an_unknown_kind_is_refused(store) -> None:
    with pytest.raises(ValueError):
        store.add("nonsense", "whatever")


def test_using_a_rule_is_counted(store) -> None:
    """A rule used 40 times is a different object from one that never fired."""
    UnattendedPolicy(store=store).approves_command("pytest")
    rule = store.get(rule_id("command", "pytest"))
    assert rule is not None and rule.fire_count == 1 and rule.last_fired_at


def test_matching_logic_is_not_configurable(store) -> None:
    """Chaining stays refused however the rule was written."""
    store.add("command", "pytest")
    policy = UnattendedPolicy(store=store)
    assert policy.approves_command("pytest; rm -rf /") is False
    assert policy.approves_command("pytest | tee out") is False


def test_no_store_falls_back_to_the_shipped_defaults() -> None:
    """A caller with no database gets north's behaviour, not an empty list."""
    policy = UnattendedPolicy()
    assert policy.approves_command("pytest") is True
    assert policy.approves_git("status") is True


# ── The new categories ───────────────────────────────────────────────────────


def test_a_reversible_device_toggle_is_in_the_list(store) -> None:
    policy = UnattendedPolicy(store=store)
    assert policy.approves_device("on") is True
    assert policy.approves_device("toggle") is True


def test_unlocking_is_not_a_toggle(store) -> None:
    """Verb by verb, never a blanket 'smart home' category."""
    policy = UnattendedPolicy(store=store)
    assert policy.approves_device("unlock") is False
    assert policy.approves_device("disarm") is False


# ── The two hard rules: not rows, and not overridable by rows ────────────────


def _policy(store) -> ApprovalPolicy:
    return ApprovalPolicy(
        mode_provider=lambda: ApprovalMode.AUTO,
        unattended=UnattendedPolicy(store=store),
    )


@pytest.mark.parametrize(
    "summary",
    ["send an email to the hiring manager", "post a reply on slack", "submit the application form"],
)
def test_sending_to_another_person_is_never_auto_approved(store, summary: str) -> None:
    action = Action(agent="general", kind=ActionKind.OTHER, summary=summary, command="notify_user", mutating=True)
    assert forbidden_reason(action)


@pytest.mark.parametrize("summary", ["pay the invoice", "book the flight", "purchase the licence"])
def test_spending_money_is_never_auto_approved(store, summary: str) -> None:
    action = Action(agent="general", kind=ActionKind.OTHER, summary=summary, command="notify_user", mutating=True)
    assert forbidden_reason(action)


@pytest.mark.asyncio
async def test_a_rule_in_the_table_cannot_authorise_a_send(store) -> None:
    """The load-bearing test for making the list editable at all.

    The table decides what is safe. It does not get to decide what is unsafe -
    otherwise one careless write, or one model with reach into the settings API,
    turns off a rule whose failure is silent and one-way.
    """
    store.add("command", "sendmail")
    action = Action(
        agent="general",
        kind=ActionKind.SHELL_COMMAND,
        summary="mail the report",
        command="sendmail",
        mutating=True,
    )

    ruling = await _policy(store).rule(action)

    assert ruling.verdict is Verdict.ASK
    assert not ruling.allowed


@pytest.mark.asyncio
async def test_the_flags_beat_an_innocuous_looking_command(store) -> None:
    """A tool that knows it reaches a third party is believed over the word list."""
    store.add("command", "deliver")
    action = Action(
        agent="general",
        kind=ActionKind.SHELL_COMMAND,
        summary="run the thing",
        command="deliver",
        mutating=True,
        reaches_third_party=True,
    )

    assert (await _policy(store).rule(action)).verdict is Verdict.ASK


@pytest.mark.asyncio
async def test_an_ordinary_safe_command_still_runs(store) -> None:
    """The hard rules must not swallow the list they are protecting."""
    action = Action(
        agent="coder", kind=ActionKind.SHELL_COMMAND, summary="run the tests", command="pytest", mutating=True
    )

    assert (await _policy(store).rule(action)).verdict is Verdict.ALLOW


@pytest.mark.asyncio
async def test_reads_never_needed_a_rule(store) -> None:
    """The policy's first tier already allows anything that changes nothing.

    Worth pinning: #4 originally proposed a 'reads' category, which would have
    been a second answer to a settled question.
    """
    action = Action(agent="general", kind=ActionKind.OTHER, summary="look something up", mutating=False)

    assert (await _policy(store).rule(action)).verdict is Verdict.ALLOW
