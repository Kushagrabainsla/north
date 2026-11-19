"""`query` reads elements by CSS selector with north's own script: the model sends values, never code.

On 2026-11-18, 72 of the 102 page scripts in one outreach task only read the page, and
each went to the approval model. `query` is that read, with nothing in it to check.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.models import ToolOutput
from tools.universal.browser import BrowserTool, _format_query, _query_output
from tools.universal.browser_query import query_script

# A page of three buttons, enough for querySelectorAll, text, attributes and visibility.
_FAKE_PAGE = """
const element = (tag, text, attributes, shown) => ({
  tagName: tag, innerText: text, textContent: text,
  getAttribute: name => (name in attributes ? attributes[name] : null),
  getClientRects: () => (shown ? [1] : []),
});
const buttons = [
  element("BUTTON", "Connect", {"aria-label": "Invite Ian R. to connect"}, true),
  element("BUTTON", "Message\\n  now", {"aria-label": "Message Ian"}, true),
  element("BUTTON", "Connect", {"aria-label": "Invite Paul to connect"}, false),
];
globalThis.document = {
  title: "Ian R. | LinkedIn",
  querySelectorAll: selector => {
    if (selector === "main button") return buttons;
    if (selector.includes("!!")) throw new Error("bad selector");
    return [];
  },
};
globalThis.location = {href: "https://www.linkedin.com/in/ian/"};
globalThis.getComputedStyle = () => ({visibility: "visible"});
"""


def _on_fake_page(tmp_path: Path, params: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node to run the page script")
    script = tmp_path / "query.js"
    script.write_text(_FAKE_PAGE + "\nconsole.log(" + query_script(params) + ");\n", encoding="utf-8")
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(done.stdout)


def test_query_reads_text_and_the_named_attributes(tmp_path) -> None:
    found = _on_fake_page(tmp_path, {"selector": "main button", "attributes": ["aria-label", "href"]})

    assert found["count"] == 3 and found["url"] == "https://www.linkedin.com/in/ian/"
    first, second, _ = found["items"]
    assert first == {"tag": "button", "text": "Connect", "visible": True, "aria-label": "Invite Ian R. to connect"}
    assert second["text"] == "Message now"  # white space folded


def test_query_keeps_only_matching_and_shown_elements(tmp_path) -> None:
    found = _on_fake_page(
        tmp_path, {"selector": "main button", "attributes": ["aria-label"], "contains": "invite", "visible_only": True}
    )

    assert found["count"] == 1
    assert [item["aria-label"] for item in found["items"]] == ["Invite Ian R. to connect"]


def test_a_bad_selector_is_reported_not_thrown(tmp_path) -> None:
    assert "not a valid CSS selector" in _on_fake_page(tmp_path, {"selector": "a!!b"})["error"]


def test_what_the_model_sends_stays_data(tmp_path) -> None:
    """A selector with quotes and code in it is a selector that matches nothing, never code that runs."""
    found = _on_fake_page(tmp_path, {"selector": '"); document.title = "x"; ("'})

    assert found["count"] == 0 and found["title"] == "Ian R. | LinkedIn"


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "selector"),
        ({"selector": "a", "attributes": ["href); alert(1"]}, "Not attribute names"),
        ({"selector": "a", "attributes": [f"a{n}" for n in range(11)]}, "at most 10"),
        ({"selector": "a", "limit": "many"}, "whole number"),
    ],
)
def test_values_the_script_cannot_use_are_refused_by_name(params, message) -> None:
    with pytest.raises(ValueError, match=message):
        query_script(params)


def test_query_only_reads_so_it_is_not_a_change() -> None:
    assert "query" not in BrowserTool._MUTATING_ACTIONS
    assert "query" in BrowserTool.parameters_schema["properties"]["action"]["enum"]


def test_a_north_profile_query_result_is_parsed_and_shown_one_line_each() -> None:
    raw = json.dumps(
        {
            "url": "https://x.test/",
            "title": "X",
            "count": 2,
            "items": [{"tag": "a", "text": "Docs", "visible": True, "href": "/docs"}],
        }
    )
    output = _query_output(ToolOutput(success=True, data={"action": "eval", "result": raw}))

    assert output.success and output.data["action"] == "query" and output.data["count"] == 2
    assert _format_query(output.data) == "URL: https://x.test/\n2 element(s) match; 1 shown.\n1. <a> Docs [href=/docs]"
    failed = _query_output(ToolOutput(success=True, data={"result": json.dumps({"error": "not a valid CSS selector"})}))
    assert not failed.success and "not a valid CSS selector" in failed.error
