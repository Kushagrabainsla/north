"""The safe-action list, edited from the page rather than a source file.

Issue #4: the list shipped as a tuple in Python, so you could not see what north
would run without asking, add to it, or remove an entry you disagreed with.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from approval.unattended_rules import UnattendedRuleStore, rule_id
from orchestrator.api_context import ApiServices, bind_services
from web import api as web_api


class _Settings:
    def __init__(self, mode: str = "auto") -> None:
        self.autonomy = mode


@pytest.fixture
def store(tmp_path):
    rules = UnattendedRuleStore(tmp_path / "approval_memory.db")
    with bind_services(ApiServices(unattended_rules=rules, north_settings=_Settings())):
        yield rules


async def test_the_list_is_visible(store) -> None:
    payload = await web_api.unattended_rules()

    assert payload["rules"], "the shipped rules must be listed, not hidden in Python"
    assert {r["pattern"] for r in payload["rules"]} >= {"pytest", "status"}
    assert payload["active"] is True


async def test_the_page_is_told_when_no_rule_can_fire(tmp_path) -> None:
    """A rule only fires in `auto`. Listing inert rules with nothing saying so
    is how someone concludes the feature is broken."""
    rules = UnattendedRuleStore(tmp_path / "approval_memory.db")
    with bind_services(ApiServices(unattended_rules=rules, north_settings=_Settings("interactive"))):
        payload = await web_api.unattended_rules()

    assert payload["active"] is False
    assert payload["mode"] == "interactive"


async def test_a_rule_can_be_added_disabled_and_deleted(store) -> None:
    created = await web_api.add_unattended_rule(web_api.UnattendedRuleCreate(kind="command", pattern="make lint"))
    assert created["source"] == "user"
    assert "make lint" in store.patterns("command")

    await web_api.update_unattended_rule(created["id"], web_api.UnattendedRuleUpdate(enabled=False))
    assert "make lint" not in store.patterns("command")

    await web_api.delete_unattended_rule(created["id"])
    assert store.get(created["id"]) is None


async def test_a_shipped_rule_survives_removal_so_it_can_be_restored(store) -> None:
    await web_api.delete_unattended_rule(rule_id("command", "pytest"))
    assert "pytest" not in store.patterns("command")

    restored = await web_api.restore_unattended_rules()

    assert restored["restored"] >= 1
    assert "pytest" in store.patterns("command")


async def test_an_unknown_kind_is_rejected_rather_than_stored(store) -> None:
    with pytest.raises(HTTPException) as exc:
        await web_api.add_unattended_rule(web_api.UnattendedRuleCreate(kind="nonsense", pattern="x"))
    assert exc.value.status_code == 422


async def test_editing_a_rule_that_does_not_exist_is_a_404(store) -> None:
    with pytest.raises(HTTPException) as exc:
        await web_api.update_unattended_rule("command:nope", web_api.UnattendedRuleUpdate(enabled=False))
    assert exc.value.status_code == 404
