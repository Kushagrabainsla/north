"""Unit checks for the live tool-awareness A/B harness."""

from experiments.tool_selection.awareness_benchmark import build_prompt, score_predictions
from experiments.tool_selection.retrieval_benchmark import EvalCase, ToolDoc


def _docs() -> dict[str, ToolDoc]:
    return {
        "web_search": ToolDoc("web_search", "Search the current web.", {}),
        "read_file": ToolDoc("read_file", "Read a workspace file.", {}),
    }


def test_catalog_arms_change_only_catalog_detail() -> None:
    case = EvalCase("research", "Research this", ("web_search",), "single")
    no_catalog = build_prompt("none", _docs(), [case])
    names = build_prompt("names", _docs(), [case])
    descriptions = build_prompt("descriptions", _docs(), [case])

    assert "research: Research this" in no_catalog
    assert "web_search" not in no_catalog
    assert "web_search" in names
    assert "Search the current web" in descriptions


def test_score_predictions_requires_every_tool_for_full_task_recall() -> None:
    cases = [EvalCase("edit", "Find and edit", ("read_file", "web_search"), "multi")]
    result = score_predictions("names", {"edit": ["read_file"]}, cases)

    assert result.tool_recall == 0.5
    assert result.full_task_recall == 0.0
