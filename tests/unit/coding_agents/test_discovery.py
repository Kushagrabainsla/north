"""An agent can probe "ready" and still fail every run: on 2026-11-18 Codex was out of credit."""

from __future__ import annotations

from pathlib import Path

import pytest

import coding_agents.discovery as discovery
from coding_agents.models import Availability, FailureKind, RunOutcome, RunSpec


class _Agent:
    def __init__(self, availability: Availability, outcome: RunOutcome | None = None) -> None:
        self.availability = availability
        self.outcome = outcome or RunOutcome(True, "ready", "s")
        self.specs: list[RunSpec] = []

    async def probe(self) -> Availability:
        return self.availability

    async def run(self, spec: RunSpec, on_event) -> RunOutcome:
        assert (Path(spec.workspace) / ".git").is_dir(), "the request runs in a repository of its own"
        self.specs.append(spec)
        return self.outcome


def _installed(monkeypatch: pytest.MonkeyPatch, **agents: _Agent) -> None:
    monkeypatch.setattr(discovery, "discover_backends", lambda: agents)


async def test_an_agent_out_of_credit_is_not_ready_and_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    out_of_credit = RunOutcome(False, "", "s", failure=FailureKind.RESOURCE, error="Your workspace is out of credits.")
    _installed(monkeypatch, codex=_Agent(Availability(True, "0.165.0"), out_of_credit))

    [codex] = await discovery.try_agents(["codex"])

    assert not codex.ready
    assert codex.detail == "codex could not answer a one-line request: Your workspace is out of credits"
    assert codex.fix == "Wait for codex's usage limit to reset, or add credit to its account."


async def test_an_agent_that_answers_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _Agent(Availability(True, "2.1.308"))
    _installed(monkeypatch, claude=agent)

    [claude] = await discovery.try_agents(["claude"])

    assert claude.ready and claude.version == "2.1.308"
    assert agent.specs[0].max_budget_usd <= 0.05, "the request spends almost nothing"


async def test_an_agent_that_stops_at_the_request_cap_after_reaching_its_model_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-11-22: Claude Code's first call cost more than the $0.05 cap, and north called it not ready."""
    capped = RunOutcome(
        False, "", "s", failure=FailureKind.LIMIT, error="Reached maximum budget ($0.05)", tokens_in=24_000
    )
    _installed(monkeypatch, claude=_Agent(Availability(True, "2.1.308"), capped))

    [claude] = await discovery.try_agents(["claude"])

    assert claude.ready and not claude.detail


async def test_a_stop_before_the_model_answered_is_still_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that timed out before any model call proves nothing about the login or the credit."""
    timed_out = RunOutcome(False, "", "s", failure=FailureKind.LIMIT, error="The run did not finish in 600 seconds.")
    _installed(monkeypatch, claude=_Agent(Availability(True, "2.1.308"), timed_out))

    [claude] = await discovery.try_agents(["claude"])

    assert not claude.ready
    assert claude.detail.startswith("claude could not answer a one-line request")


async def test_an_agent_the_probe_rejects_is_never_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _Agent(Availability(False, "2.1.308", "Claude Code is not logged in; run `claude auth login`"))
    _installed(monkeypatch, claude=agent)

    [claude, codex] = await discovery.try_agents(["claude", "codex"])

    assert not claude.ready and claude.detail.endswith("run `claude auth login`")
    assert not agent.specs
    assert not codex.installed
