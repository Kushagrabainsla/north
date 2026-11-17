"""A browser script runs however the model wrote it (issue #82, item 7).

The browser tool runs a script in REPL mode, where a top-level `return` is
"SyntaxError: Illegal return statement"; the first browser script failed that
way five times. `evaluate.js` runs such a script again as an async function
body, but only when the script provably did not run. The cases run against a
real V8 through Node's inspector, which speaks Chrome's Runtime protocol.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("page_scripts.mjs")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")


@pytest.fixture(scope="module")
def results() -> dict[str, dict]:
    done = subprocess.run(["node", str(_SCRIPT)], capture_output=True, text=True, timeout=60, check=True)
    return {row["name"]: row for row in map(json.loads, done.stdout.splitlines())}


def test_a_script_that_ends_in_an_expression_runs_once(results) -> None:
    assert results["lastExpression"]["value"] == 42
    assert results["lastExpression"]["scriptRuns"] == 1


@pytest.mark.parametrize("case", ["functionBody", "functionBodyWithAwait"])
def test_a_script_written_as_a_function_body_returns_its_value(results, case) -> None:
    assert results[case]["error"] is None
    assert results[case]["value"] == 42


def test_a_script_that_failed_while_running_is_not_run_again(results) -> None:
    """JSON.parse throws a SyntaxError after the first line ran: a second run would repeat that line."""
    assert results["runtimeSyntaxError"]["error"].startswith("SyntaxError: Expected property name")
    assert results["runtimeSyntaxError"]["scriptRuns"] == 1
    assert results["runtimeSyntaxErrorCount"]["value"] == 1


def test_a_broken_script_keeps_its_own_error(results) -> None:
    assert results["broken"]["error"].startswith("SyntaxError: Unexpected token '='")


def test_no_marker_is_left_on_the_page(results) -> None:
    assert results["markersLeft"]["value"] == 0
