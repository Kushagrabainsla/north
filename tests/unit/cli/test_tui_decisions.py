"""/decisions in the TUI shows what the approvals page shows, from the same API (#30)."""

from __future__ import annotations

import contextlib

from cli.tui import NorthApp
from cli.tui_text import decision_lines

_OVERRULED = {
    "title": "Deploy",
    "type": "approval",
    "status": "approved",
    "chosen_option": "Approve",
    "decided_by_label": "you",
    "reason": "the freeze is over",
    "memory_used": [],
    "overruled": {
        "status": "rejected",
        "decided_by_label": "the memory decider",
        "reason": "it is Friday",
        "memory_used": [{"kind": "fact", "label": "Fact", "text": "no deploys on Fridays [ever]", "ref": ""}],
    },
}


def test_a_decision_names_who_decided_why_and_what_it_used() -> None:
    text = "\n".join(decision_lines(_OVERRULED))

    assert "by you" in text and "the freeze is over" in text
    assert "you overruled north" in text and "by the memory decider" in text and "it is Friday" in text
    assert r"Fact: no deploys on Fridays \[ever]" in text, "text from memory is escaped, not read as markup"


async def test_decisions_lists_only_decided_cards() -> None:
    cards = [{"title": "Waiting", "status": "pending"}, _OVERRULED]

    class _Resp:
        status_code = 200

        def json(self):
            return cards

    class _Client:
        async def get(self, url, **kw):
            assert url.endswith("/web/api/approvals")
            return _Resp()

    @contextlib.asynccontextmanager
    async def fake_http():
        yield _Client()

    app = NorthApp(base_url="http://127.0.0.1:1", headers={})
    app._http = fake_http  # type: ignore[method-assign]
    logged: list[str] = []
    app._log = logged.append  # type: ignore[method-assign]

    await app._slash_decisions("/decisions")

    text = "\n".join(logged)
    assert "Deploy" in text and "Waiting" not in text
