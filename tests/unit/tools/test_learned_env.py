"""Tools north writes run in their own process and environment, with their own packages, in the sandbox.

`create_tool` takes the PyPI packages a tool needs, each at one exact version, and writes them in the
file's PEP 723 block. `uv` makes the tool its own environment; north's never changes. The runner loads the
tool there and serves it over MCP; north never imports its code. Under the OS sandbox the process gets
only the secrets it declared and cannot read north's home or the credential folders, so its own word that
a call changes nothing is honoured, and a view can read from it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from config import secret_store
from tools.learned_env import ToolEnvironments, find_uv, pin_problems, script_dependencies, with_dependencies
from tools.learned_remote import LearnedRuntime, RunnerTool
from tools.models import ToolInput
from tools.registry import ToolRegistry
from tools.specialized import _os_sandbox
from tools.universal.create_tool import CreateToolTool
from views.document import reference_checks

SANDBOX = _os_sandbox.current()
needs_sandbox = pytest.mark.skipif(SANDBOX is None, reason="no OS sandbox on this host")
needs_uv = pytest.mark.skipif(find_uv() is None, reason="uv is not installed")

TOOL = """
from pathlib import Path

from config.secret_store import secret
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class Lamps(Tool):
    name = "lamps"
    description = "Lists lamps, or switches one."
    parameters_schema = {"type": "object", "properties": {"action": {"type": "string"}, "path": {"type": "string"}}}
    is_mutating = True
    read_only_when = {"action": ["list"]}
    uses_secrets = ("lamp_token",)

    async def run(self, input: ToolInput) -> ToolOutput:
        action = input.params.get("action")
        if action == "list":
            return ToolOutput(success=True, data={"lamps": [{"id": "desk", "on": False}]})
        if action == "token":
            return ToolOutput(success=True, data={"length": len(secret("lamp_token"))})
        if action == "read":
            try:
                return ToolOutput(success=True, data={"read": len(Path(input.params["path"]).read_bytes())})
            except OSError as exc:
                return ToolOutput(success=True, data={"refused": type(exc).__name__})
        return ToolOutput(success=True, data={"switched": True})
"""


def _registry(tmp_path: Path) -> ToolRegistry:
    return ToolRegistry(auto_register=False, learned_dir=tmp_path / "learned" / "tools")


def _creator(registry: ToolRegistry, tmp_path: Path) -> CreateToolTool:
    return CreateToolTool(
        tool_registry=registry, tools_dir=tmp_path / "learned" / "tools", runtime=registry.learned_runtime()
    )


async def _create(tmp_path: Path, content: str = TOOL, packages=None, test_params=None):
    registry = _registry(tmp_path)
    creator = _creator(registry, tmp_path)
    params = {"action": "create", "name": "lamps", "content": content, "test_params": test_params or {"action": "list"}}
    if packages is not None:
        params["packages"] = packages
    out = await creator.run(ToolInput(params=params))
    return out, registry


@pytest.fixture
def token() -> str:
    secret_store.store().save("lamp_token", "token-123456")
    return "token-123456"


# ── Packages ────────────────────────────────────────────────────────────────


def test_packages_are_written_in_the_files_standard_block_and_read_back() -> None:
    source = with_dependencies("import kasa\n", ["python-kasa==0.12.0"])

    assert source.startswith('# /// script\n# dependencies = ["python-kasa==0.12.0"]\n# ///\n')
    assert script_dependencies(source) == ["python-kasa==0.12.0"]
    assert script_dependencies(with_dependencies(source, [])) == []


@pytest.mark.parametrize(
    "package",
    ["python-kasa", "python-kasa>=0.12", "python-kasa==0.*", "kasa @ https://example.com/k.whl", "not a package!"],
)
def test_a_package_must_be_one_exact_version_from_pypi(package: str) -> None:
    assert pin_problems([package])


def test_an_exact_version_passes() -> None:
    assert pin_problems(["python-kasa==0.12.0", "tabulate[widechars]==0.9.0"]) == []


async def test_create_tool_refuses_a_package_without_a_version(tmp_path: Path) -> None:
    out, registry = await _create(tmp_path, packages=["tabulate"])

    assert not out.success and "one exact version" in out.error
    assert "lamps" not in registry.all_tool_names()


async def test_the_card_lists_the_packages_and_its_key_changes_with_them(tmp_path: Path) -> None:
    creator = _creator(_registry(tmp_path), tmp_path)
    call = {"action": "create", "name": "lamps", "content": TOOL, "test_params": {"action": "list"}}

    one = await creator.describe(ToolInput(params={**call, "packages": ["tabulate==0.9.0"]}))
    two = await creator.describe(ToolInput(params={**call, "packages": ["tabulate==0.8.10"]}))

    assert "own process and environment" in one.message and "tabulate==0.9.0" in one.message
    assert one.action.args != two.action.args, "approving one set of packages must not cover another"


# ── Its own process ─────────────────────────────────────────────────────────


async def test_a_tool_is_created_tested_and_registered_to_run_in_its_own_process(tmp_path: Path, token) -> None:
    out, registry = await _create(tmp_path)

    assert out.success, out.error
    tool = registry.get("lamps")
    assert isinstance(tool, RunnerTool)
    saved = json.loads((tmp_path / "learned" / "tools" / "specialized" / "lamps.json").read_text())
    assert saved["meta"]["read_only_when"] == {"action": ["list"]} and saved["meta"]["uses_secrets"] == ["lamp_token"]
    assert not any(name.startswith(("north_learned_tool_", "north_tool_candidate_")) for name in sys.modules), (
        "north never imports a tool's code"
    )
    listed = await tool.run(ToolInput(params={"action": "list"}))
    assert listed.success and listed.data == {"lamps": [{"id": "desk", "on": False}]}
    await tool.aclose()


async def test_after_a_restart_it_comes_back_from_what_was_saved_with_no_import(tmp_path: Path, token) -> None:
    out, _ = await _create(tmp_path)
    assert out.success, out.error

    again = ToolRegistry(auto_register=True, learned_dir=tmp_path / "learned" / "tools")

    tool = again.get("lamps")
    assert isinstance(tool, RunnerTool) and tool.uses_secrets == ("lamp_token",)
    assert not any(name.startswith("north_learned_tool_lamps") for name in sys.modules)


async def test_it_gets_the_secret_it_declared_and_no_other(tmp_path: Path, token) -> None:
    secret_store.store().save("someone_elses", "not-for-lamps-99")
    out, registry = await _create(tmp_path)
    tool = registry.get("lamps")

    mine = await tool.run(ToolInput(params={"action": "token"}))
    argv, env = await tool.command()
    await tool.aclose()

    assert mine.success and mine.data == {"length": len(token)}
    assert json.loads(env["NORTH_TOOL_SECRETS"]) == {"lamp_token": token}, "only what it declared is passed"


def test_the_runner_takes_the_secrets_out_of_its_environment_and_refuses_any_other(monkeypatch) -> None:
    import tools.learned_runner as runner

    monkeypatch.setenv("NORTH_TOOL_SECRETS", json.dumps({"lamp_token": "token-123456"}))
    taken = runner._take_secrets()
    monkeypatch.setattr(runner, "_SECRETS", taken)

    import os

    assert "NORTH_TOOL_SECRETS" not in os.environ, "a program the tool starts does not inherit them"
    assert runner.secret("lamp_token") == "token-123456"
    with pytest.raises(runner.SecretUnavailable, match="someone_elses"):
        runner.secret("someone_elses")


async def test_a_tool_with_a_missing_package_says_which_one_to_name(tmp_path: Path, token) -> None:
    out, registry = await _create(tmp_path, content="import tabulate\n" + TOOL)

    assert not out.success
    assert "No module named 'tabulate'" in out.error and "`packages`" in out.error
    assert "lamps" not in registry.all_tool_names()


async def test_an_idle_tool_stops_its_process(tmp_path: Path, monkeypatch, token) -> None:
    import tools.learned_remote as remote

    monkeypatch.setattr(remote, "IDLE_SECONDS", 0.05)
    out, registry = await _create(tmp_path)
    tool = registry.get("lamps")
    await tool.run(ToolInput(params={"action": "list"}))
    assert tool._client is not None

    import asyncio

    await asyncio.sleep(0.5)
    assert tool._client is None, "an idle tool holds no process"


# ── Read-only calls, and the sandbox that makes them safe ─────────────────────


def _runner_tool(tmp_path: Path, sandbox) -> RunnerTool:
    meta = {"name": "lamps", "is_mutating": True, "read_only_when": {"action": ["list"]}}
    return RunnerTool(tmp_path / "lamps.py", meta, [], LearnedRuntime(tmp_path, sandbox=sandbox))


def test_its_read_only_calls_count_only_inside_the_sandbox(tmp_path: Path) -> None:
    confined = _runner_tool(tmp_path, sandbox=object())
    open_host = _runner_tool(tmp_path, sandbox=None)

    assert confined.mutates({"action": "list"}) is False and confined.mutates({"action": "on"}) is True
    assert open_host.mutates({"action": "list"}) is True, "outside the sandbox its own word is not trusted"


def test_a_view_can_read_its_list_action_once_it_runs_in_the_sandbox(tmp_path: Path) -> None:
    for sandbox, readable in ((object(), True), (None, False)):
        registry = ToolRegistry(auto_register=False)
        registry.register(_runner_tool(tmp_path, sandbox))
        tool_problem, _ = reference_checks(registry, None)

        assert (tool_problem("lamps", {"action": "list"}) is None) is readable


@needs_sandbox
async def test_in_the_sandbox_it_cannot_read_norths_home_or_the_credential_folders(tmp_path: Path, token) -> None:
    from config.settings import settings

    key = settings.north_home / "secret.key"
    key.write_text("north-server-key-000", encoding="utf-8")
    ssh = Path.home() / ".ssh"
    out, registry = await _create(tmp_path)
    tool = registry.get("lamps")

    north = await tool.run(ToolInput(params={"action": "read", "path": str(key)}))
    await tool.aclose()

    assert tool.sandboxed
    assert north.data == {"refused": "PermissionError"}
    if ssh.exists():
        tool = registry.get("lamps")
        creds = await tool.run(ToolInput(params={"action": "read", "path": str(next(ssh.iterdir(), ssh))}))
        await tool.aclose()
        assert "read" not in creds.data


# ── Real environments ───────────────────────────────────────────────────────


@needs_uv
async def test_a_tool_gets_its_own_version_of_a_package_north_also_has(tmp_path: Path, token) -> None:
    """The point of its own environment: north keeps its packaging; the tool has the one it pinned."""
    from importlib.metadata import version

    content = TOOL.replace(
        'return ToolOutput(success=True, data={"switched": True})',
        'import packaging\n        return ToolOutput(success=True, data={"packaging": packaging.__version__})',
    )
    environments = ToolEnvironments(tmp_path / "envs")
    environments.real_environments = True  # type: ignore[attr-defined]
    registry = _registry(tmp_path)
    creator = CreateToolTool(
        tool_registry=registry,
        tools_dir=tmp_path / "learned" / "tools",
        runtime=LearnedRuntime(tmp_path, sandbox=SANDBOX, environments=environments),
    )
    out = await creator.run(
        ToolInput(
            params={
                "action": "create",
                "name": "lamps",
                "content": content,
                "packages": ["packaging==23.2"],
                "test_params": {"action": "list"},
            }
        )
    )
    assert out.success, out.error
    tool = registry.get("lamps")
    result = await tool.run(ToolInput(params={"action": "switch"}))
    await tool.aclose()

    assert result.data == {"packaging": "23.2"}
    assert version("packaging") != "23.2", "north's own environment did not change"
    assert len(list((tmp_path / "envs").glob("lamps-*"))) == 1
    creator._runtime.remove("lamps")
    assert not list((tmp_path / "envs").glob("lamps-*")), "deleting a tool deletes its environment"
