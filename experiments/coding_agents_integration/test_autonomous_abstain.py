"""Question: can Autonomous decide coding requests from memory only, abstaining when no fact covers them?

Drives the real `MemoryDecider` with a stand-in memory gateway and model.
"""

from __future__ import annotations

import json

import pytest

from approval.decider import MemoryDecider
from approval.models import ApprovalDecision
from approval.policy import Action, ActionKind
from inference.models import CompletionRequest, CompletionResponse
from memory import ContextDocument, MemoryContext, MemoryPrincipal
from tests.conftest import MockInferenceRouter


class _Memory:
    def __init__(self, facts: list[str]) -> None:
        self._facts = facts

    async def principal_for(self, name, domain=None, workspace=""):  # noqa: ANN001
        return MemoryPrincipal(name=name, domain=domain, allowed_domains=frozenset())

    async def recall(self, principal, query, *, fact_limit=15, episode_limit=3) -> MemoryContext:  # noqa: ANN001
        return MemoryContext(facts=self._facts[:fact_limit])

    async def read_document(self, doc: ContextDocument) -> str:
        return ""


class _ApprovingModel(MockInferenceRouter):
    """A model that always says approve - the worst case for a tricked judge."""

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        reply = {"decision": "approve", "reason": "looks fine", "used": []}
        return CompletionResponse(text=json.dumps(reply), model_used="m", tokens_in=1, tokens_out=1, cost_usd=0.0)


def _push() -> Action:
    return Action(agent="coding:claude", kind=ActionKind.GIT, summary="push origin main", operation="push")


async def test_with_a_covering_fact_an_approving_judge_approves_and_cites_it() -> None:
    decider = MemoryDecider(_Memory(["Pushing to this repo's main is fine"]), _ApprovingModel(), None)

    answer = await decider.rule(_push())

    assert answer is not None and answer.decision == ApprovalDecision.APPROVED


@pytest.mark.xfail(
    strict=True, reason="today an approving model approves a push with no covering fact; the rule is not built"
)
async def test_with_no_covering_fact_the_decider_abstains_so_a_card_waits() -> None:
    decider = MemoryDecider(_Memory([]), _ApprovingModel(), None)

    answer = await decider.rule(_push())

    assert answer is None
