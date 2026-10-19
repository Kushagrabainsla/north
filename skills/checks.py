"""Checks a flow step's result must pass, in code, before the run counts it done.

A skill's success criteria are words in the prompt: when the model breaks one,
nothing noticed, and the run still said "completed". The Oct 17 research
briefing listed "ExecutionBridge: Adapting Frozen World-Model Planners to
Controller-Induced Execution Shift", a title the model made up; arXiv's is
"Plans Are Not Executions: Adapting ...".

A skill names the checks it wants under ``execution.checks``. The checks
themselves live here, in north's code, so a learned skill can ask for one but
never bring code of its own to run.

    checks:
    - saved_file: {field: file}
    - arxiv_papers: {field: file, days: 7, earlier: ~/.north/news/research}
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

from utils.net import fetch_url_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckSpec:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


def parse_checks(raw: Any) -> tuple[CheckSpec, ...]:
    """``execution.checks`` as specs: a list of names, or of {name: {args}}. Raises ValueError."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("execution.checks must be a list")
    specs: list[CheckSpec] = []
    for item in raw:
        name, args = (item, {}) if isinstance(item, str) else _one_entry(item)
        if name not in CHECKS:
            raise ValueError(f"Unknown check {name!r}. Known checks: {sorted(CHECKS)}")
        specs.append(CheckSpec(name, args))
    return tuple(specs)


def _one_entry(item: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(item, dict) or len(item) != 1:
        raise ValueError("each execution.checks entry is a check name, or one {name: {arguments}}")
    ((name, args),) = item.items()
    if not isinstance(args, dict):
        raise ValueError(f"the arguments of check {name!r} must be a mapping")
    return str(name), args


async def run_checks(specs: tuple[CheckSpec, ...], output: dict[str, Any], *, today: date) -> list[str]:
    """Every problem the checks find in *output*, in words the agent can act on. Empty when all pass."""
    problems: list[str] = []
    for spec in specs:
        problems.extend(await CHECKS[spec.name](output, spec.args, today))
    return problems


def _file_of(output: dict[str, Any], args: dict[str, Any]) -> Path | None:
    value = output.get(str(args.get("field", "file")))
    return Path(value).expanduser() if isinstance(value, str) and value.strip() else None


async def _saved_file(output: dict[str, Any], args: dict[str, Any], today: date) -> list[str]:
    """The output names a saved file, by absolute path, and it is not empty."""
    name = str(args.get("field", "file"))
    path = _file_of(output, args)
    if path is None:
        return [f"the result has no '{name}' path"]
    if not path.is_absolute():
        return [f"'{name}' is not an absolute path: {path}"]
    if not path.is_file() or path.stat().st_size == 0:
        return [f"the file at {path} was not saved, or is empty"]
    return []


_ARXIV_ID = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arXiv:)(\d{4}\.\d{4,5})", re.IGNORECASE)
_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_ARXIV_API = "https://export.arxiv.org/api/query"


async def _arxiv_papers(output: dict[str, Any], args: dict[str, Any], today: date) -> list[str]:
    """Each arXiv paper the file lists has its exact title, is recent, and was not sent before.

    Checked against arXiv itself, one request for every paper, rather than
    against what the agent read: a mangled query (`%26` for `&`) hands it the
    wrong papers, and those would pass a check that trusted them.
    """
    path = _file_of(output, args)
    if path is None or not path.is_file():
        return []  # saved_file says so
    text = path.read_text(encoding="utf-8")
    ids = sorted({match.group(1) for match in _ARXIV_ID.finditer(text)})
    if not ids:
        return []
    try:
        papers = await asyncio.to_thread(_arxiv_metadata, ids)
    except Exception:
        # Not knowing is not a failure of the briefing; arXiv being down must
        # not stop the morning's run. It is logged, never passed off as checked.
        logger.warning("Could not check %d papers against arXiv", len(ids), exc_info=True)
        return []
    problems: list[str] = []
    page = _normalised(text)
    oldest = today - timedelta(days=int(args.get("days", 7)))
    sent_before = _ids_in_earlier_files(args.get("earlier"), path)
    for paper_id in ids:
        paper = papers.get(paper_id)
        if paper is None:
            problems.append(f"arXiv has no paper {paper_id}")
            continue
        title, published = paper
        if _normalised(title) not in page:
            problems.append(f"{paper_id}: the title must be exactly arXiv's, {title!r}")
        if published < oldest:
            problems.append(f"{paper_id} was published {published.isoformat()}, before {oldest.isoformat()}")
        if paper_id in sent_before:
            problems.append(f"{paper_id} was already in an earlier briefing")
    return problems


def _arxiv_metadata(ids: list[str]) -> dict[str, tuple[str, date]]:
    """{id: (title, first published)} for *ids*, from the arXiv API."""
    query = urlencode({"id_list": ",".join(ids), "max_results": len(ids)})
    root = ElementTree.fromstring(fetch_url_text(f"{_ARXIV_API}?{query}").text)
    papers: dict[str, tuple[str, date]] = {}
    for entry in root.findall("a:entry", _ATOM):
        found = _ARXIV_ID.search(entry.findtext("a:id", "", _ATOM))
        title = " ".join(entry.findtext("a:title", "", _ATOM).split())
        published = entry.findtext("a:published", "", _ATOM)[:10]
        if found and title and published:
            papers[found.group(1)] = (title, date.fromisoformat(published))
    return papers


def _ids_in_earlier_files(folder: Any, current: Path) -> set[str]:
    """Papers in the files under *folder* saved before *current*: a later one was not sent first."""
    if not folder:
        return set()
    saved_at = current.stat().st_mtime
    sent: set[str] = set()
    for other in Path(str(folder)).expanduser().rglob("*.md"):
        if other.resolve() != current.resolve() and other.stat().st_mtime < saved_at:
            sent.update(match.group(1) for match in _ARXIV_ID.finditer(other.read_text(encoding="utf-8")))
    return sent


def _normalised(text: str) -> str:
    """Spacing and case do not make a different title; every word and character does."""
    return " ".join(text.split()).casefold()


CHECKS: dict[str, Callable[[dict[str, Any], dict[str, Any], date], Awaitable[list[str]]]] = {
    "saved_file": _saved_file,
    "arxiv_papers": _arxiv_papers,
}
