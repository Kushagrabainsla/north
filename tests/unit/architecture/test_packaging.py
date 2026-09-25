"""Packaging contract gate.

ADR 0004 keeps the application packages at the repository root instead of
migrating to `src/north/`. These checks pin the packaging behavior that a
migration would otherwise have had to prove: the command users type, the target
it resolves to, that every owned package is actually shipped, and that vendored
frontend dependencies stay out of the source distribution.
"""

from __future__ import annotations

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
    ):
        assert f"prune {directory}" in manifest
    assert "exclude agents/**/README.md" in manifest
    assert "exclude web/index.html" in manifest
    assert "exclude web/vite.config.js" in manifest


def test_every_built_in_flow_and_skill_file_is_declared_as_package_data(pyproject: dict) -> None:
    """A built-in that is not package data works from a checkout and vanishes from an install.

    The daily news briefing shipped as a built-in flow without being listed here,
    so an installed north had a schedule pointing at a flow that did not exist.
    """
    patterns = pyproject["tool"]["setuptools"]["package-data"]["resources"]
    resources = ROOT / "resources"

    for directory, suffix in (("builtin-flows", "*.yaml"), ("builtin-skills", "*.md")):
        shipped = [path for path in (resources / directory).rglob(suffix) if path.is_file()]
        assert shipped, f"resources/{directory} has nothing to ship"
        for path in shipped:
            relative = path.relative_to(resources).as_posix()
            covered = any(_glob_matches(relative, pattern) for pattern in patterns)
            assert covered, f"{relative} is not covered by [tool.setuptools.package-data].resources"


def _glob_matches(relative: str, pattern: str) -> bool:
    """setuptools globs are relative to the package: `dir/**/*.ext` also matches `dir/x.ext`."""
    from fnmatch import fnmatch

    head, _, tail = pattern.partition("**/")
    return relative.startswith(head) and fnmatch(relative.rsplit("/", 1)[-1], tail)
