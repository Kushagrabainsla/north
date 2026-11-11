"""North's commands cannot write a git repository's files, whatever program they use.

Reading a command could only catch the forms it knew: in a real run north edited a tracked file with a Python
heredoc, past the check for `sed -i` and `>`. The kernel decides now, so these run the real sandbox.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import approving_store, bind_approvals
from tools.models import ToolInput
from tools.specialized import _os_sandbox
from tools.specialized.bash import BashTool

pytestmark = pytest.mark.skipif(_os_sandbox.current() is None, reason="needs macOS Seatbelt")

_GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "my.repo-1"  # a dot and a dash: the path goes into a kernel regex
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text("x = 1\n")
    (root / ".gitignore").write_text("generated/\ndist/\n")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "seed"]):
        subprocess.run([*_GIT, *args], cwd=root, check=True, capture_output=True)
    return root


async def _run(command: str, workspace: Path, *, isolated_copy: bool = False):
    tool = bind_approvals(BashTool(os_sandbox=True), store=approving_store())
    return await tool.execute(
        ToolInput(
            params={"command": command, "workspace": str(workspace)},
            granted_workspace=str(workspace),
            isolated_copy=isolated_copy,
        )
    )


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"open('src/calc.py','w').write('x = 2')\"",
        "python3 - <<'EOF'\nfrom pathlib import Path\nPath('src/calc.py').write_text('x = 2')\nEOF",
        "perl -e 'open(F, q(>), q(src/calc.py)) or die $!; print F 2'",
        "cp /etc/hosts src/calc.py",
        "rm src/calc.py",
        "mv src/calc.py src/other.py",
    ],
)
async def test_no_program_can_change_a_tracked_file(repo: Path, command: str) -> None:
    out = await _run(command, repo)

    assert out.failure_kind == "refused", out
    assert "coding_agent" in (out.error or "")
    assert (repo / "src" / "calc.py").read_text() == "x = 1\n"


async def test_a_refusal_hidden_behind_a_pipe_is_still_explained(repo: Path) -> None:
    out = await _run("python3 -c \"open('src/calc.py','w').write('x')\" 2>&1 | tail -1", repo)

    assert "coding_agent" in out.data["stdout"]
    assert (repo / "src" / "calc.py").read_text() == "x = 1\n"


async def test_a_passing_command_that_prints_an_unrelated_refusal_stays_a_success(repo: Path) -> None:
    """Real run: pytest passed, printed a warning naming a folder outside the repository, and read as refused."""
    out = await _run("touch dist/a 2>/dev/null; echo 'warning: /x/y: Operation not permitted'; echo 3 passed", repo)

    assert out.success
    assert "3 passed" in out.data["stdout"]


async def test_a_new_file_in_the_repository_is_refused(repo: Path) -> None:
    out = await _run("touch src/new.py", repo)

    assert out.failure_kind == "refused"
    assert not (repo / "src" / "new.py").exists()


@pytest.mark.parametrize(
    "path", ["generated/out.txt", "dist/app.js", "src/__pycache__/calc.pyc", "node_modules/a/b.js", ".pytest_cache/v"]
)
async def test_what_git_ignores_or_tools_generate_stays_writable(repo: Path, path: str) -> None:
    out = await _run(f"mkdir -p $(dirname {path}) && touch {path}", repo)

    assert out.success, out
    assert (repo / path).exists()


async def test_git_itself_keeps_working(repo: Path) -> None:
    (repo / "src" / "calc.py").write_text("x = 3\n")  # the user's own edit

    out = await _run("git -c user.name=t -c user.email=t@t commit -qam 'ship it'", repo)

    assert out.success, out
    assert "ship it" in subprocess.run(["git", "log", "-1"], cwd=repo, capture_output=True, text=True).stdout


async def test_a_program_chained_after_git_is_still_limited(repo: Path) -> None:
    out = await _run("git status && python3 -c \"open('src/calc.py','w').write('x')\"", repo)

    assert out.failure_kind == "refused"
    assert (repo / "src" / "calc.py").read_text() == "x = 1\n"


async def test_a_coding_agents_own_copy_may_be_written_by_its_tests(repo: Path) -> None:
    out = await _run("python3 -c \"open('src/calc.py','w').write('x = 2')\"", repo, isolated_copy=True)

    assert out.success, out
    assert (repo / "src" / "calc.py").read_text() == "x = 2"


async def test_a_folder_outside_any_repository_is_not_limited(tmp_path: Path) -> None:
    out = await _run("python3 -c \"open('a.txt','w').write('x')\"", tmp_path)

    assert out.success, out
    assert (tmp_path / "a.txt").read_text() == "x"
