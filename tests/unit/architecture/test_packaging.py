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
    "architecture",
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
    assert "tests*" in set(pyproject["tool"]["setuptools"]["packages"]["find"]["exclude"])


def test_vendored_frontend_dependencies_stay_out_of_the_sdist() -> None:
    """The wheel already excluded them; the sdist shipped 717 entries anyway."""
    assert "prune web/node_modules" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
