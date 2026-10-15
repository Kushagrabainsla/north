"""The Facts tab can set when a fact is true; dates change only when the edit sends them."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memory.facts import FactStore
from orchestrator.api_context import ApiServices, bind_services
from web import api


async def _no_vectors(texts: list[str]) -> list[list[float]]:
    return [[] for _ in texts]


@pytest.mark.asyncio
async def test_a_fact_is_added_and_edited_with_its_dates(tmp_path) -> None:
    store = FactStore(db_path=tmp_path / "facts.db", embed_fn=_no_vectors)
    with bind_services(ApiServices(fact_store=store)):
        created = await api.create_memory_fact(
            api.FactCreate(content="User interns at LinkedIn", category="jobs", valid_until="2026-08-14")
        )
        assert (created["category"], created["valid_until"]) == ("jobs", "2026-08-14")

        kept = await api.update_memory_fact(
            created["id"], api.FactCreate(content="User interns at LinkedIn in Mountain View", category="jobs")
        )
        assert kept["valid_until"] == "2026-08-14"

        cleared = await api.update_memory_fact(
            created["id"], api.FactCreate(content=kept["content"], category="jobs", valid_until=None)
        )
        assert cleared["valid_until"] is None


def test_a_date_must_be_a_day() -> None:
    with pytest.raises(ValidationError):
        api.FactCreate(content="User starts at Acme", valid_from="next Monday")
