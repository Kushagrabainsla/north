"""An empty search is tried once more: ddgs picks its sites at random, and some answer nothing."""

from __future__ import annotations

import pytest
from ddgs.exceptions import DDGSException

import tools.universal.web_search as web_search
from tools.models import ToolInput
from tools.universal.web_search import WebSearchTool

_HIT = [{"title": "AI stocks lead a rebound", "snippet": "Wall Street...", "url": "https://apnews.com/a"}]


def _tool(monkeypatch: pytest.MonkeyPatch, answers: list) -> tuple[WebSearchTool, list[str]]:
    tool = WebSearchTool()
    asked: list[str] = []

    def fake_search(query: str, max_results: int) -> list[dict]:
        asked.append(query)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(tool, "_search", fake_search)
    monkeypatch.setattr(web_search.asyncio, "sleep", no_wait)
    return tool, asked


@pytest.mark.asyncio
async def test_an_empty_search_is_tried_again_and_its_results_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ddgs 9.16, 9 of 24 news searches came back empty; with the retry, 2."""
    tool, asked = _tool(monkeypatch, [DDGSException("No results found."), _HIT])

    output = await tool.run(ToolInput(params={"query": "AI technology news October 19 2026"}))

    assert output.success and output.data["results"] == _HIT
    assert len(asked) == 2


@pytest.mark.asyncio
async def test_a_search_empty_twice_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    tool, asked = _tool(monkeypatch, [DDGSException("No results found."), DDGSException("No results found.")])

    output = await tool.run(ToolInput(params={"query": "zzqq"}))

    assert not output.success and output.error == "Search failed: No results found."
    assert len(asked) == 2


@pytest.mark.asyncio
async def test_a_search_with_results_is_asked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    tool, asked = _tool(monkeypatch, [_HIT])

    assert (await tool.run(ToolInput(params={"query": "python release"}))).success
    assert len(asked) == 1


def test_the_search_library_stays_on_the_version_that_finds_results() -> None:
    """Upgraded to 9.16.0, it found nothing for 9 of 24 searches; 9.14.4 for none of 36."""
    import ddgs

    assert ddgs.__version__ == "9.14.4"
