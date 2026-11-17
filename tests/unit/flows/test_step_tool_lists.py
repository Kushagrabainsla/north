"""A flow step has the tools its work needs (issue #82, item 4).

A step can use only the tools its skill lists. The research briefing was told to
call read_tool_output for a long result and could not, in all 4 runs. The news
briefing called fetch_url in every run, and the outreach step was told to call
search_files; neither tool was in the list. Now every step has read_tool_output,
and a flow whose step text names a tool the step cannot use is rejected.
"""

from __future__ import annotations

from flows.models import Flow, FlowStep
from flows.validation import STEP_TOOLS, step_tools, validate_flow_capabilities
from skills.models import Skill, SkillExecution
from utils.tools import named_tools


class _Registry:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]


class _Tools:
    """The tools north has, by name."""

    def __init__(self, *names: str) -> None:
        self.names = set(names)

    def get(self, name):
        if name not in self.names:
            raise KeyError(name)
        return name

    def all_tool_names(self):
        return set(self.names)


_TOOLS = _Tools("bash", "browser", "fetch_url", "read_tool_output", "web_search", "write_file")


def _execution(*tools: str) -> SkillExecution:
    return SkillExecution(
        agent="general",
        tools=tools,
        approval="never",
        inputs={"type": "object"},
        outputs={"type": "object"},
        success_criteria=("Done",),
    )


def _check(tmp_path, *, tools: tuple[str, ...], body: str = "1. Do it.", instructions: str = ""):
    skill = Skill(
        name="brief",
        description="Use when writing a briefing.",
        body=body,
        directory=tmp_path,
        domains=frozenset({"general"}),
        execution=_execution(*tools),
    )
    flow = Flow(
        name="demo",
        description="One step",
        directory=tmp_path,
        steps=(FlowStep(name="one", skill="brief", instructions=instructions, approval="never"),),
    )
    return validate_flow_capabilities(flow, skill_registry=_Registry({"brief": skill}), tool_registry=_TOOLS)


def test_every_step_has_read_tool_output() -> None:
    assert step_tools(_execution("web_search", "write_file"), {}) == ("web_search", "write_file", *STEP_TOOLS)
    assert "read_tool_output" in STEP_TOOLS


def test_a_skill_that_lists_read_tool_output_does_not_get_it_twice() -> None:
    assert step_tools(_execution("read_tool_output", "bash"), {}) == ("read_tool_output", "bash")


def test_a_skill_that_names_a_tool_it_does_not_list_is_rejected(tmp_path) -> None:
    report = _check(tmp_path, tools=("web_search", "write_file"), body="Read each article with `fetch_url`.")

    assert not report.valid
    assert report.errors == (
        "step 1 ('one'): its text names tool 'fetch_url', which skill 'brief' does not list; "
        "add 'fetch_url' to the skill's tools or take it out of the text",
    )


def test_step_instructions_that_name_an_unlisted_tool_are_rejected(tmp_path) -> None:
    report = _check(tmp_path, tools=("browser", "write_file"), instructions="Look up old logs with bash first.")

    assert not report.valid
    assert "names tool 'bash'" in report.errors[0]


def test_a_retired_name_counts_as_the_tool_that_replaced_it(tmp_path) -> None:
    """The outreach step says search_files; it lists bash, which does that work now."""
    assert _check(tmp_path, tools=("browser", "bash"), instructions="search_files the notes store.").valid

    report = _check(tmp_path, tools=("browser",), instructions="search_files the notes store.")
    assert "names tool 'search_files (now bash)'" in report.errors[0]
    assert "add 'bash'" in report.errors[0]


def test_read_tool_output_may_be_named_without_being_listed(tmp_path) -> None:
    assert _check(tmp_path, tools=("web_search",), body="Call read_tool_output for the rest.").valid


def test_a_skill_that_names_only_its_own_tools_is_valid(tmp_path) -> None:
    body = "Search with web_search, read the article with fetch_url, save with write_file."
    assert _check(tmp_path, tools=("web_search", "fetch_url", "write_file"), body=body).valid


def test_a_word_inside_a_longer_word_is_not_a_tool_name() -> None:
    assert named_tools("Run a global search, then rebash nothing.", {"glob", "bash"}) == set()
    assert named_tools("Use `bash` and fetch_url.", {"bash", "fetch_url", "gh"}) == {"bash", "fetch_url"}
