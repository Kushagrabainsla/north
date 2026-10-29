"""The nightly check - lint, the whole suite, the live coding-agent tests - run by north on itself.

launchd used to start scripts/smoke.sh at 03:30. macOS keeps background jobs out
of the Desktop, Documents and Downloads folders, and north's project sat on the
Desktop: from Oct 7 every night ended "Operation not permitted" before the
script began, so the failure it exists to report was never reported. The north
server can read the folder it was started from, so it runs the script itself.

The live stage drives the installed Claude Code and Codex, the agents north
hands its coding to. They update often, and an update that changes their flags
or output breaks north's coding where no test with a fake can see it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from coding_agents.discovery import check_agents
from config.settings import settings
from inference.exceptions import WaitingFor
from utils.power import fully_awake
from utils.processes import stop_process_group
from utils.time import localnow

logger = logging.getLogger(__name__)

# Written by scripts/install_smoke_schedule.sh, run from the project folder:
# north can live anywhere, and only a machine with its project folder can check it.
CHECKOUT_FILE_NAME = "checkout"
_TIME_LIMIT_S = 60 * 60

# A failure names its causes, most common first, so the run says what broke
# without opening a report of thousands of lines. The report keeps the rest.
_CAUSES_SHOWN = 5
_CAUSE_CHARS = 240
_STAGE_LINES_SHOWN = 8
_FAILED_STAGE_RE = re.compile(r"^--- (.+): FAILED$", re.MULTILINE)


def checkout_file() -> Path:
    return settings.north_home / "smoke" / CHECKOUT_FILE_NAME


def registered_checkout() -> Path | None:
    """The project folder the nightly check runs in, or None when none is registered."""
    try:
        folder = Path(checkout_file().read_text(encoding="utf-8").strip()).expanduser()
    except OSError:
        return None
    return folder if (folder / "scripts" / "smoke.sh").is_file() else None


async def nightly_check() -> str:
    """Run scripts/smoke.sh with the live tests; the summary on a pass, an error naming what failed otherwise."""
    folder = registered_checkout()
    if folder is None:
        raise RuntimeError(
            "No north project folder is registered for the nightly check. "
            "Run scripts/install_smoke_schedule.sh from the project folder."
        )
    # A closed laptop wakes for seconds at a time; the check takes minutes and
    # would be cut off. It waits for a real wake instead, and runs then.
    if not fully_awake():
        raise WaitingFor("an awake laptop", "The laptop is asleep; the nightly check runs as soon as it is awake.")
    report = settings.north_home / "smoke" / f"{localnow():%Y-%m-%d_%H%M}.log"
    # The lowest CPU priority: after a night asleep it can run while you work.
    process = await asyncio.create_subprocess_exec(
        "nice",
        "-n",
        "19",
        str(folder / "scripts" / "smoke.sh"),
        cwd=folder,
        env={**os.environ, "SMOKE_LIVE": "1", "NORTH_HOME": str(settings.north_home), "SMOKE_REPORT": str(report)},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # its own process group, so stopping it stops the tests it started
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), _TIME_LIMIT_S)
    except TimeoutError:
        raise RuntimeError(f"The nightly check did not finish within {_TIME_LIMIT_S // 60} minutes.") from None
    finally:
        await stop_process_group(process, grace_seconds=0)
    last = next((line for line in reversed(output.decode(errors="replace").splitlines()) if line.strip()), "")
    # Killed before its verdict - north stopped mid-check on Oct 20 - is no result,
    # not a failure: the run waits, and runs again once north is back.
    cut_off = process.returncode is not None and process.returncode < 0
    causes = await asyncio.to_thread(_take_results, report, process.returncode != 0 and not cut_off)
    if cut_off:
        raise WaitingFor(
            "an uninterrupted check",
            f"The nightly check was stopped by signal {-process.returncode} before it finished, so it has no "
            "result. It runs again by itself.",
        )
    if process.returncode != 0:
        headline = f"The nightly check {last.removeprefix('smoke ') or 'failed'}"
        raise RuntimeError("\n".join([headline, *await _coding_agent_lines(), *causes]))
    return f"The nightly check {last.removeprefix('smoke ') or 'passed'}"


async def _coding_agent_lines() -> list[str]:
    """Each coding agent north can hand work to, and what one that cannot take a run needs.

    The live stage runs every installed agent; when one is not ready, all of
    its tests fail for that one reason. Its own probe says what it needs, such
    as "Claude Code is not logged in; run `claude auth login`".
    """
    agents = [agent for agent in await check_agents() if agent.installed]
    lines = [
        f"- {agent.name}: ready{f', {agent.version}' if agent.version else ''}"
        if agent.ready
        else f"- {agent.name}: not ready. {agent.detail}"
        for agent in agents
    ]
    return ["Coding agents:", *lines] if lines else []


def _take_results(report: Path, failed: bool) -> list[str]:
    """The failure causes when the check failed. The JUnit files are read once, here; the report is what stays."""
    causes = failure_causes(report) if failed else []
    for junit in report.parent.glob(f"{report.stem}.*.xml"):
        junit.unlink(missing_ok=True)
    return causes


def failure_causes(report: Path) -> list[str]:
    """What each failed stage of a smoke report failed on, in a few lines.

    A pytest stage writes JUnit XML beside the report, and its failures are
    grouped by their error, so 16 tests failing for one reason read as one
    line. Another stage shows the first lines of its output. The 11:45 check
    on 2026-10-22 said only "failed: suite live coding agents" and a report
    path, though all 18 failures were one cause: Claude Code was logged out.
    """
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines: list[str] = []
    for stage in _FAILED_STAGE_RE.findall(text):
        junit = report.with_name(f"{report.stem}.{stage.replace(' ', '-')}.xml")
        try:
            lines += _pytest_causes(stage, junit) if junit.is_file() else _stage_output(stage, text)
        except (OSError, ElementTree.ParseError):
            logger.debug("nightly check: could not read the %s results", stage, exc_info=True)
    return lines


def _pytest_causes(stage: str, junit: Path) -> list[str]:
    root = ElementTree.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    total = sum(int(suite.get("tests", 0)) for suite in suites)
    tests_by_cause: dict[str, list[str]] = {}
    for case in root.iter("testcase"):
        problem = next((child for child in case if child.tag in {"failure", "error"}), None)
        if problem is None:
            continue
        message = (problem.get("message") or problem.text or "no message").strip()
        cause = message.splitlines()[0][:_CAUSE_CHARS] if message else "no message"
        tests_by_cause.setdefault(cause, []).append(f"{case.get('classname', '')}::{case.get('name', '')}")
    failed = sum(len(tests) for tests in tests_by_cause.values())
    ranked = sorted(tests_by_cause.items(), key=lambda item: -len(item[1]))
    shown = [f"- {len(tests)} × {cause} (e.g. {tests[0]})" for cause, tests in ranked[:_CAUSES_SHOWN]]
    more = len(ranked) - _CAUSES_SHOWN
    return [f"{stage}: {failed} of {total} tests failed", *shown, *([f"- and {more} other causes"] if more > 0 else [])]


def _stage_output(stage: str, text: str) -> list[str]:
    start = text.find(f"=== {stage} (")
    end = text.find(f"--- {stage}: FAILED")
    if start < 0 or end < 0:
        return []
    output = [line for line in text[start:end].splitlines()[1:] if line.strip()]
    return [f"{stage}:", *output[:_STAGE_LINES_SHOWN]]
