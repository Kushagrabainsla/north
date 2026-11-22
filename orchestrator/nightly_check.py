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

A failed check says each problem and its fix (CODING_STYLE 13.5). From Oct 27 to
Nov 21 every check failed, mostly because Claude Code was logged out or Codex was
out of credit; each card held a page of test output, and Resume fixed nothing.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from coding_agents.discovery import Readiness, try_agents
from config.settings import settings
from inference.exceptions import WaitingFor
from utils.power import fully_awake
from utils.processes import stop_process_group
from utils.time import format_local, localnow

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
# The commit of the last check that passed, so a failure can say which commits could have broken it.
_LAST_PASS_FILE_NAME = "last_pass.json"
# The live tests run an agent only when its variable is "1" (tests/live).
_LIVE_VARIABLE = "NORTH_LIVE_{name}"
_LIVE_TESTS = "tests/live/"


@dataclass(frozen=True)
class Problem:
    """One thing the check found: what is wrong, and what to do about it."""

    issue: str
    fix: str


class NightlyCheckFailed(RuntimeError):
    """The check found problems; its message lists each one with its fix."""

    def __init__(self, problems: list[Problem], report: Path) -> None:
        self.problems = problems
        super().__init__(_card_text(problems, report))


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
    """Run scripts/smoke.sh with the live tests; the summary on a pass, each problem with its fix otherwise."""
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
    # An agent that cannot take a run would fail every one of its tests for that one reason,
    # and hide a real failure among them. It is named with its fix, and its tests do not run.
    agents = [agent for agent in await try_agents() if agent.installed]
    live = {_LIVE_VARIABLE.format(name=agent.name.upper()): "1" if agent.ready else "0" for agent in agents}
    report = settings.north_home / "smoke" / f"{localnow():%Y-%m-%d_%H%M}.log"
    # The lowest CPU priority: after a night asleep it can run while you work.
    process = await asyncio.create_subprocess_exec(
        "nice",
        "-n",
        "19",
        str(folder / "scripts" / "smoke.sh"),
        cwd=folder,
        env={
            **os.environ,
            **live,
            "SMOKE_LIVE": "1",
            "NORTH_HOME": str(settings.north_home),
            "SMOKE_REPORT": str(report),
        },
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
    failed = process.returncode != 0 and not cut_off
    broken, silent = await asyncio.to_thread(_take_results, report, failed, cut_off)
    if cut_off:
        raise WaitingFor(
            "an uninterrupted check",
            f"The nightly check was stopped by signal {-process.returncode} before it finished, so it has no "
            "result. It runs again by itself.",
        )
    problems = [_agent_problem(agent) for agent in agents if not agent.ready]
    if broken:
        since = await asyncio.to_thread(_since_last_pass, folder)
        problems += [_code_problem(failure, folder, since) for failure in broken]
    elif failed:
        problems.append(Problem(f"The nightly check {last.removeprefix('smoke ') or 'failed'}.", _READ_REPORT))
    if silent:
        problems.append(_silent_problem(silent))
    if problems:
        raise NightlyCheckFailed(problems, report)
    await asyncio.to_thread(_record_pass, folder)
    tested = f" at {installed[1][:7]}" if installed else ""
    return f"The nightly check{tested} {last.removeprefix('smoke ') or 'passed'}"


_READ_REPORT = "Read the report named below to see what failed."


def _agent_problem(agent: Readiness) -> Problem:
    """A coding agent that cannot take a run: its tests did not run, and what it needs."""
    skipped = f"{agent.name} cannot take a coding run, so north did not test it tonight"
    if agent.fix:
        return Problem(f"{skipped}. {agent.detail}.", agent.fix)
    return Problem(f"{skipped}.", f"{agent.detail}.")


def _code_problem(failure: Failure, folder: Path, since: str) -> Problem:
    """Tests that failed for one cause: north's own code broke, and how to see it."""
    broke = f"A change in north broke it{since}."
    if not failure.tests:
        return Problem(f"{failure.stage} failed: {failure.cause}", f"{broke} {_READ_REPORT}")
    count = len(failure.tests)
    test = failure.tests[0]
    live = "NORTH_LIVE_CLAUDE=1 NORTH_LIVE_CODEX=1 " if test.startswith(_LIVE_TESTS) else ""
    issue = f"{'1 test' if count == 1 else f'{count} tests'} in {failure.stage} failed: {failure.cause}"
    return Problem(
        f"{issue} (for example {test}).",
        f"{broke} In {folder}, run `{live}.venv/bin/python -m pytest {test}`.",
    )


def _silent_problem(silent: list[str]) -> Problem:
    named = ", ".join(silent)
    return Problem(
        f"{len(silent)} live test(s) could not tell for {_INCONCLUSIVE_RUNS_ALLOWED} runs in a row, because the "
        f"agent declined to run the check: {named}.",
        "Read the request each test sends; if the agent keeps declining, make the request plainer.",
    )


def _card_text(problems: list[Problem], report: Path) -> str:
    """Each problem and its fix, numbered; the report's path, never the report."""
    count = len(problems)
    lines = [f"The nightly check found {count} problem{'' if count == 1 else 's'}."]
    for number, problem in enumerate(problems, start=1):
        lines += ["", f"{number}. Problem: {problem.issue}", f"   Fix: {problem.fix}"]
    return "\n".join([*lines, "", f"Report: {report}"])


def _git(folder: Path, *arguments: str) -> str:
    """git's output in *folder*, or "" when it fails."""
    done = subprocess.run(["git", "-C", str(folder), *arguments], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def _last_pass_file() -> Path:
    return settings.north_home / "smoke" / _LAST_PASS_FILE_NAME


def _record_pass(folder: Path) -> None:
    commit = _git(folder, "rev-parse", "HEAD")
    if commit:
        record = {"commit": commit, "at": localnow().isoformat()}
        _last_pass_file().write_text(json.dumps(record), encoding="utf-8")


def _since_last_pass(folder: Path) -> str:
    """Which commits could have broken the check, as words to follow "broke it"."""
    try:
        record = json.loads(_last_pass_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return " (no passing check is on record yet)"
    commit, at = str(record.get("commit", "")), str(record.get("at", ""))
    when = format_local(datetime.datetime.fromisoformat(at), "%b %d") if at else "an earlier night"
    count = _git(folder, "rev-list", "--count", f"{commit}..HEAD")
    if not count:
        return f" after the last pass ({commit[:7]}, {when})"
    commits = "the one commit" if count == "1" else f"one of the {count} commits"
    return f" in {commits} since the last pass ({commit[:7]}, {when})"


def _take_results(report: Path, failed: bool, cut_off: bool = False) -> tuple[list[Failure], list[str]]:
    """The failures by cause, and the tests that said nothing too many runs in a row.

    The JUnit files are read once, here; the report is what stays. A run cut off before
    its verdict counts for nothing, so it leaves the inconclusive counts alone.
    """
    broken = failures(report) if failed else []
    silent = [] if cut_off else _still_inconclusive(report)
    for junit in report.parent.glob(f"{report.stem}.*.xml"):
        junit.unlink(missing_ok=True)
    return broken, silent


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


@dataclass(frozen=True)
class Failure:
    """Failures of one stage for one cause; *tests* as pytest ids, empty for a stage that is not pytest."""

    stage: str
    cause: str
    tests: tuple[str, ...] = ()


def failures(report: Path) -> list[Failure]:
    """What each failed stage of a smoke report failed on, grouped by cause, most common first.

    A pytest stage writes JUnit XML beside the report, and its failures are
    grouped by their error, so 16 tests failing for one reason read as one
    problem. Another stage gives the first lines of its output. The 11:45 check
    on 2026-10-22 said only "failed: suite live coding agents" and a report
    path, though all 18 failures were one cause: Claude Code was logged out.
    """
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[Failure] = []
    for stage in _FAILED_STAGE_RE.findall(text):
        junit = report.with_name(f"{report.stem}.{stage.replace(' ', '-')}.xml")
        try:
            found += _pytest_failures(stage, junit) if junit.is_file() else _stage_failure(stage, text)
        except (OSError, ElementTree.ParseError):
            logger.debug("nightly check: could not read the %s results", stage, exc_info=True)
    return found


def _pytest_failures(stage: str, junit: Path) -> list[Failure]:
    tests_by_cause: dict[str, list[str]] = {}
    for case in ElementTree.parse(junit).getroot().iter("testcase"):
        problem = next((child for child in case if child.tag in {"failure", "error"}), None)
        if problem is None:
            continue
        message = (problem.get("message") or problem.text or "no message").strip()
        cause = message.splitlines()[0][:_CAUSE_CHARS] if message else "no message"
        tests_by_cause.setdefault(cause, []).append(_pytest_id(case))
    ranked = sorted(tests_by_cause.items(), key=lambda item: -len(item[1]))
    shown = [Failure(stage, cause, tuple(tests)) for cause, tests in ranked[:_CAUSES_SHOWN]]
    rest = ranked[_CAUSES_SHOWN:]
    if rest:
        count = sum(len(tests) for _, tests in rest)
        shown.append(Failure(stage, f"{count} more test(s), for {len(rest)} other causes; see the report"))
    return shown


def _pytest_id(case: ElementTree.Element) -> str:
    """The id pytest runs one test by: tests.live.test_x + test_y -> tests/live/test_x.py::test_y."""
    module = case.get("classname", "")
    return f"{module.replace('.', '/')}.py::{case.get('name', '')}"


def _stage_failure(stage: str, text: str) -> list[Failure]:
    start = text.find(f"=== {stage} (")
    end = text.find(f"--- {stage}: FAILED")
    if start < 0 or end < 0:
        return [Failure(stage, "it printed nothing")]
    output = [line.strip() for line in text[start:end].splitlines()[1:] if line.strip()]
    return [Failure(stage, " / ".join(output[:_STAGE_LINES_SHOWN])[:_CAUSE_CHARS] or "it printed nothing")]
