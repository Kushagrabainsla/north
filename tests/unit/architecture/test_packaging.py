"""Packaging contract gate.

ADR 0004 keeps the application packages at the repository root instead of
migrating to `src/north/`. These checks pin the packaging behavior that a
migration would otherwise have had to prove: the command users type, the target
it resolves to, that every owned package is actually shipped, and that vendored
frontend dependencies stay out of the source distribution.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]

# Packages that must remain importable for the installed application to work.
# Deliberately explicit: a package silently dropped from discovery only fails at
# runtime, on whichever machine installed the wheel.
REQUIRED_PACKAGES = (
    "agents",
    "approval",
    "bootstrap",
    "cli",
    "config",
    "context",
    "gateways",
    "inference",
    "jobs",
    "ledger",
    "mcp",
    "memory",
    "orchestrator",
    "resources",
    "skills",
    "tools",
    "utils",
    "web",
)


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_console_script_name_and_target_are_unchanged(pyproject: dict) -> None:
    """`north` is the public entry point; its module target must stay importable."""
    scripts = pyproject["project"]["scripts"]

    assert scripts == {"north": "cli.main:app"}

    module, _, attribute = scripts["north"].partition(":")
    assert (ROOT / Path(module.replace(".", "/")).with_suffix(".py")).is_file()
    assert attribute == "app"


def test_every_required_package_is_present_and_not_excluded(pyproject: dict) -> None:
    find = pyproject["tool"]["setuptools"]["packages"]["find"]
    excluded = set(find["exclude"])

    for package in REQUIRED_PACKAGES:
        directory = ROOT / package
        assert directory.is_dir(), f"{package} is missing"
        assert any(directory.rglob("*.py")), f"{package} ships no modules"
        assert package not in excluded
        assert f"{package}*" not in excluded


def test_namespace_discovery_stays_enabled(pyproject: dict) -> None:
    """`mcp/` has no `__init__.py`, so it ships only as a namespace package.

    Setting `namespaces = false` would silently drop it from the wheel, and the
    failure would surface at runtime in `tools/specialized/mcp_tool.py`, on
    whichever machine installed it.
    """
    find = pyproject["tool"]["setuptools"]["packages"]["find"]

    assert find.get("namespaces", True) is True
    assert not (ROOT / "mcp" / "__init__.py").exists()
    assert any((ROOT / "mcp").glob("*.py"))


def test_tests_are_never_shipped(pyproject: dict) -> None:
    excluded = set(pyproject["tool"]["setuptools"]["packages"]["find"]["exclude"])
    assert {"tests*", "docs*", "architecture*", "evals*", "experiments*", "scripts*"} <= excluded


def test_runtime_package_data_is_explicit(pyproject: dict) -> None:
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert "*" not in package_data
    assert "architecture" not in package_data
    assert package_data["resources"] == [
        "prompts/*.md",
        "policies/*.md",
        "builtin-skills/**/*.md",
        "builtin-flows/**/*.yaml",
        "network-makers.json.gz",
        "taskwindow/LICENSE",
        "taskwindow/package*.json",
        "taskwindow/extension/**/*",
    ]
    assert package_data["agents"] == ["**/*.yaml", "**/prompts/*.md"]
    assert all("README" not in pattern for pattern in package_data["agents"])


def test_vendored_frontend_dependencies_stay_out_of_the_sdist() -> None:
    """Development-only trees stay out of source distributions."""
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for directory in (
        "web/node_modules",
        ".github",
        "architecture",
        "build",
        "docs",
        "evals",
        "experiments",
        "scripts",
        "tests",
        "web/src",
        "web/public",
    ):
        assert f"prune {directory}" in manifest
    assert "exclude agents/**/README.md" in manifest
    assert "exclude web/index.html" in manifest
    assert "exclude web/vite.config.js" in manifest


def test_every_resource_file_is_declared_as_package_data(pyproject: dict) -> None:
    """A resource that is not package data works from a checkout and vanishes from an install.

    The daily news briefing shipped as a built-in flow without being listed here,
    so an installed north had a schedule pointing at a flow that did not exist; the
    table of network-card makers was left out the same way, so an installed north
    named no device's maker on the Devices page.
    """
    patterns = pyproject["tool"]["setuptools"]["package-data"]["resources"]
    resources = ROOT / "resources"
    shipped = [path.relative_to(resources).as_posix() for path in resources.rglob("*") if _is_data(path)]

    assert {"builtin-flows/nightly-check/FLOW.yaml", "builtin-skills/adding-a-home-device/SKILL.md"} <= set(shipped)
    for relative in shipped:
        covered = any(_glob_matches(relative, pattern) for pattern in patterns)
        assert covered, f"resources/{relative} is not covered by [tool.setuptools.package-data].resources"


def test_every_dashboard_file_is_declared_as_package_data(pyproject: dict) -> None:
    """The dashboard's build ships whole: its logo and app icons as much as its code.

    Only .html, .css, .js and .woff2 were declared, so the icons the build copies
    from web/public would have been served from a checkout and missing, with no
    error, from an installed north.
    """
    patterns = pyproject["tool"]["setuptools"]["package-data"]["web"]
    web = ROOT / "web"
    built = [path.relative_to(web).as_posix() for path in (web / "dist").rglob("*") if path.is_file()]

    assert {"dist/index.html", "dist/favicon.svg", "dist/manifest.webmanifest"} <= set(built)
    for relative in built:
        covered = any(_glob_matches(relative, pattern) for pattern in patterns)
        assert covered, f"web/{relative} is not covered by [tool.setuptools.package-data].web"


def _is_data(path: Path) -> bool:
    """A file a package reads at run time: not its code, which ships as a module, nor Python's cache."""
    return path.is_file() and path.suffix not in {".py", ".pyc"} and "__pycache__" not in path.parts


def test_the_glob_matcher_reads_patterns_the_way_setuptools_does() -> None:
    assert _glob_matches("builtin-flows/news.yaml", "builtin-flows/**/*.yaml")
    assert _glob_matches("builtin-flows/a/b/news.yaml", "builtin-flows/**/*.yaml")
    assert _glob_matches("network-makers.json.gz", "network-makers.json.gz")
    assert _glob_matches("taskwindow/package-lock.json", "taskwindow/package*.json")
    assert not _glob_matches("prompts/sub/x.md", "prompts/*.md")
    assert not _glob_matches("builtin-flows/news.yml", "builtin-flows/**/*.yaml")


def _glob_matches(relative: str, pattern: str) -> bool:
    """setuptools globs are relative to the package: `*` stays in one folder, `**/` spans none or several."""
    parts = re.split(r"(\*\*/|\*|\?)", pattern)
    wildcards = {"**/": "(?:.+/)?", "*": "[^/]*", "?": "[^/]"}
    return re.fullmatch("".join(wildcards.get(part, re.escape(part)) for part in parts), relative) is not None
