"""The nightly check - lint, the whole suite, the live coding-agent tests - run by north on itself.

launchd used to start scripts/smoke.sh at 03:30. macOS keeps background jobs out
of the Desktop, Documents and Downloads folders, and north's project sat on the
Desktop: from Oct 7 every night ended "Operation not permitted" before the
script began, so the failure it exists to report was never reported. The north
server can read the folder it was started from, so it runs the script itself.

The live stage drives the installed Claude Code and Codex, the agents north
hands its coding to. They update often, and an update that changes their flags
or output breaks north's coding where no test with a fake can see it.

It tests the commit north is installed from, in a clean copy of its own. On
Nov 9 it was testing a project folder 123 commits behind what was running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ElementTree
from importlib import metadata
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

_SOURCE_DIR_NAME = "src"
_SETUP_TIME_LIMIT_S = 15 * 60
# A live test that could not tell (a model declined to try) is not a failure, and not a pass.
# It fails the check once it has said nothing for this many runs in a row.
_INCONCLUSIVE_PREFIX = "inconclusive:"
_INCONCLUSIVE_RUNS_ALLOWED = 3
_INCONCLUSIVE_FILE_NAME = "inconclusive.json"


def checkout_file() -> Path:
    return settings.north_home / "smoke" / CHECKOUT_FILE_NAME


def installed_commit() -> tuple[str, str] | None:
    """The repository and commit this north was installed from (PEP 610); None when it was not from git."""
    try:
        recorded = metadata.distribution("north").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not recorded:
        return None
    data = json.loads(recorded)
    commit = (data.get("vcs_info") or {}).get("commit_id")
    return (str(data["url"]), str(commit)) if commit else None


async def _setup_command(arguments: list[str], cwd: Path, *, needs_network: bool = False) -> None:
    """Run one step of preparing the copy; an error names the step, and a network failure waits for the network."""
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), _SETUP_TIME_LIMIT_S)
    finally:
        await stop_process_group(process, grace_seconds=0)
    if process.returncode == 0:
        return
    detail = output.decode(errors="replace").strip()[-400:]
    if needs_network:
        raise WaitingFor("the network", f"The nightly check could not reach its repository: {detail}")
    raise RuntimeError(f"The nightly check could not prepare its copy of north ({arguments[0]}): {detail}")


async def _copy_of_commit(url: str, commit: str) -> Path:
    """A clean copy of north at *commit*, with its own dependencies. Kept between nights, so only changes download."""
    folder = settings.north_home / "smoke" / _SOURCE_DIR_NAME
    folder.parent.mkdir(parents=True, exist_ok=True)
    if not (folder / ".git").is_dir():
        shutil.rmtree(folder, ignore_errors=True)
        await _setup_command(
            ["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", url, str(folder)],
            folder.parent,
            needs_network=True,
        )
    await _setup_command(["git", "fetch", "--quiet", "origin", commit], folder, needs_network=True)
    await _setup_command(["git", "checkout", "--quiet", "--force", "--detach", commit], folder)
    await _setup_command(["git", "clean", "-fdqx", "--exclude=.venv"], folder)
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    await _setup_command([uv, "sync", "--quiet", "--locked"], folder, needs_network=True)
    return folder


def registered_checkout() -> Path | None:
    """The project folder the nightly check runs in, or None when none is registered."""
    try:
        folder = Path(checkout_file().read_text(encoding="utf-8").strip()).expanduser()
    except OSError:
        return None
    return folder if (folder / "scripts" / "smoke.sh").is_file() else None


async def nightly_check() -> str:
    """Run scripts/smoke.sh with the live tests; the summary on a pass, an error naming what failed otherwise."""
    # A closed laptop wakes for seconds at a time; the check takes minutes and
    # would be cut off. It waits for a real wake instead, and runs then.
    if not fully_awake():
        raise WaitingFor("an awake laptop", "The laptop is asleep; the nightly check runs as soon as it is awake.")
    installed = installed_commit()
    folder = await _copy_of_commit(*installed) if installed else registered_checkout()
    if folder is None:
        raise RuntimeError(
            "No north project folder is registered for the nightly check. "
            "Run scripts/install_smoke_schedule.sh from the project folder."
        )
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
    causes, silent = await asyncio.to_thread(_take_results, report, process.returncode != 0 and not cut_off, cut_off)
    if cut_off:
        raise WaitingFor(
            "an uninterrupted check",
            f"The nightly check was stopped by signal {-process.returncode} before it finished, so it has no "
            "result. It runs again by itself.",
        )
    if process.returncode != 0:
        headline = f"The nightly check {last.removeprefix('smoke ') or 'failed'}"
        raise RuntimeError("\n".join([headline, *await _coding_agent_lines(), *causes]))
    if silent:
        named = ", ".join(silent)
        raise RuntimeError(
            f"The nightly check could not tell for {_INCONCLUSIVE_RUNS_ALLOWED} runs in a row: {named}. "
            "Each time the agent declined to run the check."
        )
    tested = f" at {installed[1][:7]}" if installed else ""
    return f"The nightly check{tested} {last.removeprefix('smoke ') or 'passed'}"


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


def _take_results(report: Path, failed: bool, cut_off: bool = False) -> tuple[list[str], list[str]]:
    """The failure causes, and the tests that said nothing too many runs in a row.

    The JUnit files are read once, here; the report is what stays. A run cut off before
    its verdict counts for nothing, so it leaves the inconclusive counts alone.
    """
    causes = failure_causes(report) if failed else []
    silent = [] if cut_off else _still_inconclusive(report)
    for junit in report.parent.glob(f"{report.stem}.*.xml"):
        junit.unlink(missing_ok=True)
    return causes, silent


def _inconclusive_tests(report: Path) -> list[str]:
    """The tests in this run that could not tell, by name."""
    found: list[str] = []
    for junit in report.parent.glob(f"{report.stem}.*.xml"):
        try:
            cases = ElementTree.parse(junit).getroot().iter("testcase")
        except (OSError, ElementTree.ParseError):
            logger.debug("nightly check: could not read %s", junit, exc_info=True)
            continue
        for case in cases:
            skipped = case.find("skipped")
            if skipped is not None and (skipped.get("message") or "").lstrip().startswith(_INCONCLUSIVE_PREFIX):
                found.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return found


def _still_inconclusive(report: Path) -> list[str]:
    """Count this run's inconclusive tests in a row; the ones that reached the limit. A test that told resets."""
    counts_file = report.parent / _INCONCLUSIVE_FILE_NAME
    try:
        earlier = json.loads(counts_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        earlier = {}
    counts = {name: int(earlier.get(name, 0)) + 1 for name in _inconclusive_tests(report)}
    counts_file.write_text(json.dumps(counts, indent=2), encoding="utf-8")
    return sorted(name for name, runs in counts.items() if runs >= _INCONCLUSIVE_RUNS_ALLOWED)


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
