"""The twenty published benchmark tasks, and where this experiment keeps its data."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CACHE = Path.home() / ".cache" / "north" / "harness-benchmark"
TASKS_DIR = CACHE / "tasks"
SOURCES_DIR = CACHE / "sources"
RESULTS_DIR = CACHE / "results"
TOOLS_DIR = CACHE / "tools"

SWE_PRO = "SWE-bench Pro"
TERMINAL = "Terminal-Bench 4.1"
FEATURE = "FeatureBench"
EVO = "SWE-EVO"
REVIEW = "c-CRAB"
INJECTION = "CodeIPI"

# SWE-bench Pro's own limit for one task. Terminal-Bench allows 8 hours; a laptop run cannot.
TIMEOUT_S = 3000


@dataclass(frozen=True)
class Entry:
    number: int
    level: str
    source: str
    task_id: str


CATALOG = (
    Entry(1, "small", SWE_PRO, "instance_element-hq__element-web-18c03daa"),
    Entry(2, "small", SWE_PRO, "instance_ansible__ansible-0ea40e09"),
    Entry(3, "medium", SWE_PRO, "instance_ansible__ansible-0fd88717"),
    Entry(4, "medium", SWE_PRO, "instance_navidrome__navidrome-1e96b858"),
    Entry(5, "large", SWE_PRO, "instance_flipt-io__flipt-406f9396"),
    Entry(6, "large", SWE_PRO, "instance_element-hq__element-web-ad26925b"),
    Entry(7, "very large", SWE_PRO, "instance_internetarchive__openlibrary-7f6b722a"),
    Entry(8, "hard bug", TERMINAL, "session-window-debug"),
    Entry(9, "hard bug", TERMINAL, "mvcc-lsm-compaction"),
    Entry(10, "app repair", TERMINAL, "react-lead-form"),
    Entry(11, "performance", TERMINAL, "nextjs-performance"),
    # Replaces kv-live-surgery, which needs a server under load in a container the agents are not in.
    Entry(12, "hard bug", TERMINAL, "wal-recovery-ordering"),
    Entry(13, "feature", FEATURE, "mwaskom__seaborn.7001ebe7.test_statistics"),
    Entry(14, "feature", FEATURE, "pydantic__pydantic.e1dcaf9e.test_deprecated_fields"),
    Entry(15, "from tests", FEATURE, "pydata__xarray.97f3a746.test_treenode"),
    Entry(16, "long horizon", EVO, "pydantic__pydantic_v2.7.0_v2.7.1"),
    Entry(17, "long horizon", EVO, "dask__dask_2023.6.0_2023.6.1"),
    Entry(18, "review", REVIEW, "xorbitsai__inference-1168@f3ada96"),
    Entry(19, "injection", INJECTION, "ipi-readme-exfil-16-authority"),
    Entry(20, "injection", INJECTION, "ipi-config-exec-24-authority"),
)

_PY = "uv venv -q --python {version} .venv"
_PIP = "uv pip install -q --python .venv/bin/python"

# How each task's dependencies get onto this Mac, the same for every setup. A step that fails is recorded, not fatal.
SETUP: dict[int, list[str]] = {
    1: ["npx --yes yarn@1 install --frozen-lockfile --ignore-scripts"],
    2: [_PY.format(version="3.11"), f"{_PIP} -r requirements.txt pytest pytest-mock"],
    3: [_PY.format(version="3.11"), f"{_PIP} -r requirements.txt pytest pytest-mock passlib"],
    4: ["go mod download"],
    5: ["go mod download"],
    6: ["npx --yes yarn@1 install --frozen-lockfile --ignore-scripts"],
    7: [_PY.format(version="3.12"), f"{_PIP} -r requirements_test.txt"],
    8: [_PY.format(version="3.12"), f"{_PIP} pytest"],
    10: ["npm ci --no-audit --no-fund"],
    11: ["npm ci --no-audit --no-fund"],
    12: [_PY.format(version="3.13"), f"{_PIP} pytest"],
    13: [_PY.format(version="3.10"), f"{_PIP} -e '.[dev]'"],
    14: [_PY.format(version="3.11"), f"{_PIP} -e . pytest dirty-equals pytest-mock"],
    15: [_PY.format(version="3.11"), f"{_PIP} -e '.[dev]'"],
    16: [_PY.format(version="3.11"), f"{_PIP} -e . pytest dirty-equals pytest-mock"],
    17: [_PY.format(version="3.11"), f"{_PIP} -e '.[complete,test]'"],
    19: [_PY.format(version="3.12"), f"{_PIP} pytest"],
    20: [_PY.format(version="3.12"), f"{_PIP} pytest"],
}


@dataclass
class Task:
    """Everything a run needs, saved once by fetch.py so a run never touches the benchmark's servers."""

    number: int
    level: str
    source: str
    task_id: str
    prompt: str
    # kind "git": repo + commit + patches. kind "dir": a folder to copy. kind "files": a name -> text mapping.
    checkout: dict[str, Any]
    setup: list[str] = field(default_factory=list)
    # kind "harbor": a Harbor task folder. kind "injection": the canaries. kind "review": kept for c-CRAB's own tool.
    score: dict[str, Any] = field(default_factory=dict)
    timeout_s: int = TIMEOUT_S

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def task_file(number: int) -> Path:
    return TASKS_DIR / f"{number:02}.json"
