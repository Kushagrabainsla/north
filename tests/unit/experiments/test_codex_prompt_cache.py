"""The replay counts only what Codex can serve: equal leading tools, then the instructions."""

from datetime import datetime, timedelta

from experiments.codex_prompt_cache.replay import SESSION_KEYS, RunTools, estimate_reuse, shared_prefix_tokens

_SIZES = {"browser": 800, "create_flow": 700, "fetch_url": 600, "use_skill": 400}
_START = datetime(2026, 10, 22, 12, 0)


def _run(tools: tuple[str, ...], *, task: str = "task_a", minutes: float = 0) -> RunTools:
    return RunTools(
        started=_START + timedelta(minutes=minutes),
        task_id=task,
        tools=tools,
        instructions_tokens=5_000,
        first_call_tokens=10_000,
    )


def test_an_identical_tool_set_reuses_the_tools_and_the_instructions() -> None:
    tools = ("browser", "fetch_url", "use_skill")

    assert shared_prefix_tokens(_run(tools), _run(tools), _SIZES) == 1_800 + 5_000


def test_a_different_first_tool_reuses_nothing() -> None:
    """The probe: one changed first tool gave 0 cached in the same session."""
    assert shared_prefix_tokens(_run(("browser", "use_skill")), _run(("create_flow", "use_skill")), _SIZES) == 0


def test_leading_tools_count_until_the_first_difference_and_not_the_instructions() -> None:
    previous = _run(("browser", "create_flow", "use_skill"))
    current = _run(("browser", "create_flow", "fetch_url", "use_skill"))

    assert shared_prefix_tokens(previous, current, _SIZES) == 1_500


def test_a_shared_prefix_under_the_cache_minimum_is_not_served() -> None:
    assert shared_prefix_tokens(_run(("browser", "use_skill")), _run(("browser", "fetch_url")), _SIZES) == 0


def test_a_run_reuses_only_the_last_run_under_its_key_within_the_cache_life() -> None:
    tools = ("browser", "fetch_url", "use_skill")
    runs = [_run(tools, task="a"), _run(tools, task="b", minutes=2), _run(tools, task="b", minutes=30)]

    per_task = estimate_reuse(runs, SESSION_KEYS["per task"], ttl_minutes=10, tool_tokens=_SIZES)
    per_agent = estimate_reuse(runs, SESSION_KEYS["per agent"], ttl_minutes=10, tool_tokens=_SIZES)
    per_run = estimate_reuse(runs, SESSION_KEYS["per run (today)"], ttl_minutes=60, tool_tokens=_SIZES)

    assert (per_task.cached_tokens, per_task.runs_helped) == (0, 0)
    assert (per_agent.cached_tokens, per_agent.runs_helped) == (6_800, 1)
    assert per_run.cached_tokens == 0
    assert per_agent.first_call_tokens == 30_000
