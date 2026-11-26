"""#64 before/after: real dev commands and safety checks under the Codex profile north builds.

Usage: e64_sandbox_probes.py <label> <checkout>

The profile comes from *checkout*'s own `CodexBackend._profile`, so `main` and the branch are each
measured with their own code. Each command runs under `codex sandbox` with that profile: Codex's
own sandbox, with no model call. When the checkout's backend gives each run a temp folder (the
branch does), the probe does the same, as its `run` does: TMPDIR is that folder.

The fixture is a copy-like folder in north's real copies folder (`~/.cache/north/coding-worktrees`),
next to a second "copy" with a canary file, plus a canary in `~/.config`. All are deleted after.
The results hold probe names, exit codes and counts only: no paths and no output.
"""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

LABEL, CHECKOUT = sys.argv[1], Path(sys.argv[2]).resolve()
sys.path.insert(0, str(CHECKOUT))

from coding_agents import CodexBackend, Mode, RunSpec  # noqa: E402
from coding_agents.environment import in_project  # noqa: E402

HOME = Path.home()
COPIES = HOME / ".cache" / "north" / "coding-worktrees"
RESULTS = Path(__file__).resolve().parent / "results"
MARKERS = ("operation not permitted", "read-only file system")
TAG = f"north-e64-{uuid.uuid4().hex[:8]}"
WORKSPACE = COPIES / f"{TAG}-ws"
OTHER = COPIES / f"{TAG}-other"
CONFIG_CANARY = HOME / ".config" / f"{TAG}-canary.txt"
MADE_IN_HOME = HOME / f"{TAG}-made"
MADE_IN_COPIES = HOME / ".cache" / "north" / f"{TAG}-made"

PACKAGE = {
    "name": "e64",
    "version": "1.0.0",
    "scripts": {
        "test": "node -e \"require('fs').realpathSync(process.cwd()); console.log('tests ok')\"",
        "build": "node -e \"console.log('built')\"",
    },
}

# (name, modes, command, expected): "ok" is a command a dev run needs; "refused" is a safety check.
PROBES: list[tuple[str, tuple[str, ...], str, str]] = [
    ("npm test", ("plan", "edit"), "npm test --silent", "ok"),
    ("npm run build", ("plan", "edit"), "npm run build --silent", "ok"),
    ("virtualenv python", ("plan", "edit"), ".venv/bin/python -c 'print(42)'", "ok"),
    ("python3 from the virtualenv", ("plan", "edit"), "python3 -c 'import sys; print(sys.prefix)'", "ok"),
    ("git status", ("plan", "edit"), "git status --short", "ok"),
    ("python heredoc", ("plan", "edit"), "python3 - <<'PY'\nprint(7)\nPY", "ok"),
    ("write in the workspace", ("edit",), "touch made.txt", "ok"),
    ("read another copy", ("plan", "edit"), f"cat ../{OTHER.name}/canary.txt", "refused"),
    ("read north's secret", ("plan", "edit"), "cat ~/.north/secret.key > /dev/null", "refused"),
    ("list ~/.ssh", ("plan", "edit"), "ls ~/.ssh > /dev/null", "refused"),
    ("read another ~/.config file", ("plan", "edit"), f"cat ~/.config/{CONFIG_CANARY.name}", "refused"),
    ("write another copy", ("edit",), f"touch ../{OTHER.name}/x", "refused"),
    ("write the copies folder", ("edit",), f"touch ~/.cache/north/{MADE_IN_COPIES.name}", "refused"),
    ("write the home folder", ("edit",), f"touch ~/{MADE_IN_HOME.name}", "refused"),
    ("write in the workspace (plan)", ("plan",), "touch made.txt", "refused"),
    ("write /tmp (plan)", ("plan",), f"touch /tmp/{TAG}-plan", "refused"),
]


def setup() -> None:
    WORKSPACE.mkdir(parents=True)
    OTHER.mkdir(parents=True)
    (OTHER / "canary.txt").write_text("canary\n")
    CONFIG_CANARY.write_text("canary\n")
    (WORKSPACE / "package.json").write_text(json.dumps(PACKAGE))
    (WORKSPACE / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git = ["git", "-c", "user.name=e64", "-c", "user.email=e64@example.com"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "start"]):
        subprocess.run([*git, *args], cwd=WORKSPACE, check=True)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(WORKSPACE / ".venv")], check=True)


def cleanup() -> None:
    for path in (WORKSPACE, OTHER):
        shutil.rmtree(path, ignore_errors=True)
    for path in (CONFIG_CANARY, MADE_IN_HOME, MADE_IN_COPIES, Path(f"/tmp/{TAG}-plan")):
        path.unlink(missing_ok=True)


def toml(value: object) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def sandbox_argv(config: dict, command: str) -> list[str]:
    name = config["default_permissions"]
    table = config["permissions"][name]
    return [
        "codex",
        "sandbox",
        "-c",
        f"permissions.{name}={toml(table)}",
        "-c",
        f'default_permissions="{name}"',
        "-P",
        name,
        "-C",
        str(WORKSPACE.resolve()),
        "--",
        "/bin/zsh",
        "-c",
        command,
    ]


def run_mode(mode: str) -> list[dict]:
    backend = CodexBackend()
    spec = RunSpec(task="probe", workspace=str(WORKSPACE.resolve()), session_id="probe", mode=Mode(mode))
    env = in_project(backend._environment, spec.workspace)  # what the backend starts the agent with
    scratch = None
    if "scratch" in inspect.signature(backend._profile).parameters:
        from coding_agents.codex import _temp_environment

        scratch = tempfile.mkdtemp(prefix="north-codex-tmp-")
        env.update(_temp_environment(scratch))
        config = backend._profile(spec, scratch)
    else:
        config = backend._profile(spec)
    rows = []
    try:
        for name, modes, command, expected in PROBES:
            if mode not in modes:
                continue
            done = subprocess.run(
                sandbox_argv(config, command), capture_output=True, text=True, timeout=120, env=env, cwd=WORKSPACE
            )
            output = (done.stdout + done.stderr).lower()
            refusal_lines = sum(1 for line in output.splitlines() if any(m in line for m in MARKERS))
            refused = done.returncode != 0 and refusal_lines > 0
            passed = done.returncode == 0 if expected == "ok" else refused
            rows.append(
                {
                    "mode": mode,
                    "probe": name,
                    "expected": expected,
                    "exit": done.returncode,
                    "refusal_lines": refusal_lines,
                    "as_expected": passed,
                }
            )
            subprocess.run(["git", "checkout", "-q", "--", "."], cwd=WORKSPACE, check=False)
            (WORKSPACE / "made.txt").unlink(missing_ok=True)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    return rows


def main() -> None:
    commit = subprocess.run(
        ["git", "-C", str(CHECKOUT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    )
    setup()
    try:
        rows = run_mode("plan") + run_mode("edit")
    finally:
        cleanup()
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"e64_probes_{LABEL}.json"
    out.write_text(json.dumps({"label": LABEL, "commit": commit.stdout.strip(), "rows": rows}, indent=1) + "\n")
    for row in rows:
        mark = "ok " if row["as_expected"] else "BAD"
        print(f"{mark} {row['mode']:4} {row['probe']:30} exit={row['exit']:3} refusal_lines={row['refusal_lines']}")
    print("written", out)


if __name__ == "__main__":
    main()
