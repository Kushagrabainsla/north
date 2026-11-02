"""What north says about a file, a schedule, its memory, a model call or its dials is what is so.

Issue #76: read_file ended every slice, a one-line read of line 2500 included, with
"(truncated at 10000 lines)", and on the direct path that note was the whole reply
(A19). write_file said "Created" for writes that replaced a file's content (A20).
The dashboard's Memory tile said "4 context documents" over an empty memory, and
its Flows tile "0 scheduled" beside two built-ins under the Flows page's Next up
(P12). Every routing record of a call answered first time read "0 endpoints · 0
called" (P13). `north status` said "power cruise" while a pinned model decided
(P14).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import main
from inference.decisions import SUCCESS, RoutingDecision
from inference.routing.router import _finish
from jobs.cron_store import UserCronStore
from memory import ContextDocument, FileContextStore
from memory.facts import FactStore
from orchestrator.api.cron import list_cron_entries
from orchestrator.api_context import ApiServices, bind_services
from tools.models import ToolInput
from tools.universal.read_file import ReadFileTool
from tools.universal.write_file import WriteFileTool


async def test_a_slice_says_where_it_sits_in_the_file(tmp_path: Path) -> None:
    log = tmp_path / "medium.txt"
    log.write_text("".join(f"entry {n}\n" for n in range(1, 3001)))
    tool = ReadFileTool()

    out = await tool.run(ToolInput(params={"path": str(log), "start_line": 2500, "end_line": 2500}))

    text = tool.format_output(out.data)
    assert "truncated at" not in text
    assert text.endswith("… (lines 2500-2500 of 3000)")


async def test_a_write_that_replaced_a_file_says_so(tmp_path: Path) -> None:
    plan = tmp_path / "plan.txt"
    plan.write_text("the old plan")
    tool = WriteFileTool()

    replaced = await tool.run(ToolInput(params={"path": str(plan), "content": "new"}))
    created = await tool.run(ToolInput(params={"path": str(tmp_path / "fresh.txt"), "content": "new"}))

    assert tool.format_output(replaced.data).startswith("Replaced the content of")
    assert tool.format_output(created.data).startswith("Created")


async def test_the_dashboard_counts_the_schedules_the_flows_page_lists(tmp_path: Path) -> None:
    from web.api import _scheduled_count

    cron = UserCronStore(tmp_path / "cron.db")
    with bind_services(ApiServices(cron_store=cron)):
        listed = [entry for entry in await list_cron_entries() if entry.enabled]

    assert _scheduled_count(await cron.list()) == len(listed) > 0


async def test_the_dashboard_counts_what_memory_holds(tmp_path: Path) -> None:
    from web.api import _memory_counts

    async def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    facts = FactStore(db_path=tmp_path / "facts.db", embed_fn=embed)
    documents = FileContextStore(tmp_path / "context")
    with bind_services(ApiServices(fact_store=facts, context_store=documents)):
        assert await _memory_counts() == {"facts": 0, "documents": 0, "of_documents": 2}
        await facts.add_fact("User drinks tea", category="preferences")
        await documents.write(ContextDocument.NORTH_STARS, "Ship north by December.")
        assert await _memory_counts() == {"facts": 1, "documents": 1, "of_documents": 2}


def test_a_call_answered_first_time_counts_the_endpoint_that_answered() -> None:
    decision = RoutingDecision(part="planner", requirements={})
    decision.chose("claude-haiku-5-5", "anthropic")

    recorded = _finish(decision, SimpleNamespace(considered=1, skipped=[]))

    assert (recorded.outcome, recorded.endpoints, recorded.attempted) == (SUCCESS, 1, 1)


def test_status_names_the_pinned_model_not_power(monkeypatch) -> None:
    answers = {
        "/health": {"status": "ok", "checks": {}},
        "/orchestrator/settings": {
            "power": "cruise",
            "autonomy": "ask",
            "routing": "manual",
            "model": "anthropic:claude-haiku-5-5",
        },
    }
    monkeypatch.setattr(main, "_api", lambda method, path, **kw: SimpleNamespace(json=lambda: answers.get(path, {})))

    output = CliRunner().invoke(main.app, ["status"]).output

    assert "anthropic:claude-haiku-5-5 (pinned; power not in use)" in output
    assert "power      cruise" not in output
