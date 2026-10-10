"""Real kernel and vendor-executor probes; offline disposable build fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
HOST_HOME = Path.home().resolve()
ARMS = ("current", "ports_home", "isolated_paths_env", "isolated_git_objects")


def run_command(argv: list[str], *, cwd: Path, env: dict, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def environment(cache: Path, temporary: Path, *, secret: bool) -> dict:
    # Deliberately do not copy os.environ or alter HOME/CODEX_HOME. Credential
    # inheritance is tested using one synthetic token, never the user's keys.
    # npm rejects loading the same /dev/null file as both user and global config.
    for name in ("npm-user.conf", "npm-global.conf"):
        (cache / name).touch(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
        "TMPDIR": str(temporary),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "UV_NO_CONFIG": "1",
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": str(cache / "uv"),
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "npm_config_cache": str(cache / "npm"),
        "npm_config_userconfig": str(cache / "npm-user.conf"),
        "npm_config_globalconfig": str(cache / "npm-global.conf"),
        "npm_config_update_notifier": "false",
        "CLANG_MODULE_CACHE_PATH": str(cache / "clang"),
    }
    if secret:
        env["NORTH_TRIAL_CREDENTIAL"] = "synthetic-environment-token"
    return env


def wheel(path: Path) -> None:
    files = {
        "tiny_trial/__init__.py": "value = 42\n",
        "tiny_trial-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: tiny-trial\nVersion: 1.0\n",
        "tiny_trial-1.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: north-trial\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    files["tiny_trial-1.0.dist-info/RECORD"] = (
        "".join(f"{name},,\n" for name in files) + "tiny_trial-1.0.dist-info/RECORD,,\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"fixture-response")

    def log_message(self, *args) -> None:
        pass


def http(port: int) -> str:
    return shlex.join(
        [
            sys.executable,
            "-c",
            "import urllib.request; "
            "o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            f"print(o.open('http://127.0.0.1:{port}', timeout=2).read().decode(), end='')",
        ]
    )


def runtime_reads() -> list[Path]:
    paths = [Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]
    for name in ("uv", "node", "npm"):
        if executable := shutil.which(name):
            resolved = Path(executable).resolve()
            # npm/node need the bundled runtime libraries; uv is standalone.
            paths.append(resolved if name == "uv" else resolved.parent.parent)
    return paths


def profiles(
    home: Path, workspace: Path, cache: Path, temporary: Path, common_git: Path, admin: Path, allowed_ports: list[int]
) -> dict[str, str]:
    from tools.specialized import _seatbelt

    with patch.object(Path, "home", return_value=home):
        current = _seatbelt.profile(str(workspace), writable=True, proxy_port=allowed_ports[0])
    ports = " ".join(f'(remote ip "localhost:{port}")' for port in allowed_ports)
    narrow = current.replace('(remote ip "localhost:*")', ports)
    narrow += f"\n(deny file-read-data (subpath {_seatbelt._quote(home)}))"
    narrow += (
        "\n(allow file-read-data "
        + " ".join(f"(subpath {_seatbelt._quote(path)})" for path in (workspace, home / ".cache", home / ".local/bin"))
        + ")"
    )
    strict_lines = []
    write_roots = (workspace, cache, temporary, admin)
    for line in current.replace('(remote ip "localhost:*")', ports).splitlines():
        if line.startswith("(allow file-write*") and "(subpath " in line:
            line = "(allow file-write* " + " ".join(f"(subpath {_seatbelt._quote(p)})" for p in write_roots) + ")"
        strict_lines.append(line)
    strict_lines.append(
        "(deny file-read-data " + " ".join(f"(subpath {_seatbelt._quote(p)})" for p in (home, HOST_HOME)) + ")"
    )
    reads = [workspace, cache, temporary, common_git, *runtime_reads()]
    strict_lines.append("(allow file-read-data " + " ".join(f"(subpath {_seatbelt._quote(p)})" for p in reads) + ")")
    strict_lines.append(f"(deny file-write* (literal {_seatbelt._quote(workspace / '.git')}))")
    strict = "\n".join(strict_lines)
    return {"current": current, "ports_home": narrow, "isolated_paths_env": strict, "isolated_git_objects": strict}


def fixture(root: Path) -> tuple[Path, Path, Path, Path, Path, dict]:
    home = root / "synthetic-home"
    owner = home / "owner"
    owner.mkdir(parents=True)
    workspace = home / "copy"
    cache = home / "run-cache"
    temporary = root / "run-temp"
    for path in (cache, temporary, home / ".cache", home / ".local/bin", home / ".config"):
        path.mkdir(parents=True)
    env = environment(cache, temporary, secret=False)
    (owner / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (owner / "test_calc.py").write_text("from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n")
    git = ["git", "-c", "user.name=Trial", "-c", "user.email=trial@example.invalid"]
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["commit", "-qm", "fixture"],
        ["worktree", "add", "-q", "-b", "trial", str(workspace)],
    ):
        result = run_command([*git, *args], cwd=owner, env=env)
        assert result.returncode == 0, result.stderr
    admin = Path((workspace / ".git").read_text().split(": ", 1)[1].strip()).resolve()
    # New content forces git add to store a blob; unchanged content is a weak control.
    (workspace / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
    for name in ("existing-secret", ".cache/credential", ".config/credential", ".local/bin/credential"):
        (home / name).write_text("synthetic-file-token")
    (workspace / "secret-link").symlink_to(home / "existing-secret")
    (workspace / "outside-link").symlink_to(home / "outside-output")
    (workspace / "main.c").write_text("int main(void) { return 0; }\n")
    (workspace / "dependency").mkdir()
    (workspace / "dependency/package.json").write_text(
        json.dumps({"name": "trial-dependency", "version": "1.0.0", "main": "index.js"})
    )
    (workspace / "dependency/index.js").write_text("module.exports = 42;\n")
    wheel(workspace / "tiny_trial-1.0-py3-none-any.whl")
    return home, workspace, cache, temporary, admin, env


def cases(home: Path, workspace: Path, arm: str, ports: list[int]) -> list[tuple[str, str, bool, str | None]]:
    python = shlex.quote(sys.executable)
    target = workspace / arm
    target.mkdir()
    (target / "node").mkdir()
    (target / "node/package.json").write_text(
        json.dumps(
            {
                "name": "trial-app",
                "version": "1.0.0",
                "dependencies": {"trial-dependency": "file:" + str(workspace / "dependency")},
            }
        )
    )
    venv = target / "venv"
    q = shlex.quote
    jobs = [
        ("python_stdlib", f"{python} -c 'import json,sqlite3; print(json.dumps(42))'", True, "42\n"),
        ("pytest", f"{python} -m pytest test_calc.py -q -p no:cacheprovider", True, None),
        ("git_status", "git status --porcelain", True, None),
        ("git_add_worktree_index", "git add calc.py", True, None),
        ("git_diff", "git diff -- calc.py", True, None),
        ("git_stage_and_read", "git add calc.py && git show :calc.py", True, "def add(a, b):\n    return a + b + 0\n"),
        ("workspace_write", f"printf ok > {q(str(target / 'output'))}", True, None),
        ("allowed_proxy_listener", http(ports[0]), True, "fixture-response"),
        ("approved_test_server", http(ports[1]), True, "fixture-response"),
        ("blocked_control_api", http(ports[2]), False, "fixture-response"),
        ("late_home_secret", f"/bin/cat {q(str(home / 'late-secret'))}", False, "synthetic-file-token"),
        ("symlink_read_escape", f"/bin/cat {q(str(workspace / 'secret-link'))}", False, "synthetic-file-token"),
        ("global_cache_credential", f"/bin/cat {q(str(home / '.cache/credential'))}", False, "synthetic-file-token"),
        (
            "toolchain_neighbor_credential",
            f"/bin/cat {q(str(home / '.local/bin/credential'))}",
            False,
            "synthetic-file-token",
        ),
        ("outside_workspace_write", f"printf bad > {q(str(home / 'outside-output'))}", False, None),
        ("symlink_write_escape", f"printf bad > {q(str(workspace / 'outside-link'))}", False, None),
        (
            "inherited_credential",
            f'{python} -c \'import os; print(os.environ.get("NORTH_TRIAL_CREDENTIAL", "ABSENT"))\'',
            False,
            "synthetic-environment-token\n",
        ),
    ]
    if clang := shutil.which("clang"):
        jobs.append(
            (
                "c_build_and_run",
                f"{q(clang)} main.c -o {q(str(target / 'main'))} && {q(str(target / 'main'))}",
                True,
                None,
            )
        )
    if node := shutil.which("node"):
        jobs.append(
            (
                "node_runtime",
                shlex.join([node, "-e", "if (require('path').basename('/a/b') !== 'b') process.exit(1)"]),
                True,
                None,
            )
        )
        if npm := shutil.which("npm"):
            install = shlex.join(
                [
                    npm,
                    "install",
                    "--offline",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--prefix",
                    str(target / "node"),
                ]
            )
            check = shlex.join(
                [
                    node,
                    "-e",
                    "if (require("
                    + json.dumps(str(target / "node/node_modules/trial-dependency"))
                    + ") !== 42) process.exit(1)",
                ]
            )
            jobs.append(("npm_offline_install", install + " && " + check, True, None))
    if uv := shutil.which("uv"):
        create = shlex.join([uv, "venv", "--offline", "--python", sys.executable, str(venv)])
        install = shlex.join(
            [
                uv,
                "pip",
                "install",
                "--offline",
                "--no-index",
                "--no-cache",
                "--python",
                str(venv / "bin/python"),
                str(workspace / "tiny_trial-1.0-py3-none-any.whl"),
            ]
        )
        check = shlex.join([str(venv / "bin/python"), "-c", "import tiny_trial; assert tiny_trial.value == 42"])
        jobs.append(("uv_offline_wheel_install", create + " && " + install + " && " + check, True, None))
    return jobs


def redact(text: str, root: Path) -> str:
    return text.replace(str(root), "<fixture>").replace(str(HOST_HOME), "<host-home>")[-1500:]


def suite(root: Path, repeats: int = 3) -> dict:
    from tools.specialized import _seatbelt

    if not _seatbelt.available():
        return {"status": "unavailable"}
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(3)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    ports = [server.server_port for server in servers]
    rows = []
    try:
        for repeat in range(repeats):
            control_directory = root / f"control-{repeat}"
            control_directory.mkdir()
            control_home, control_workspace, control_cache, control_temp, _, _ = fixture(control_directory)
            (control_home / "late-secret").write_text("synthetic-file-token")
            controls = {}
            control_env = environment(control_cache, control_temp, secret=True)
            for name, command, _, output in cases(control_home, control_workspace, "control", ports):
                control = run_command(["/bin/sh", "-c", command], cwd=control_workspace, env=control_env)
                controls[name] = control.returncode == 0 and (output is None or control.stdout == output)
                assert controls[name], (name, control.stderr)
            # Separate state per arm; earlier successful installs cannot warm or
            # satisfy a later arm's build/installation correctness checks.
            for arm in ARMS:
                directory = root / f"sandbox-{repeat}-{arm}"
                directory.mkdir()
                home, workspace, cache, temporary, admin, _ = fixture(directory)
                variants = profiles(home, workspace, cache, temporary, home / "owner/.git", admin, ports[:2])
                (home / "late-secret").write_text("synthetic-file-token")
                env = environment(cache, temporary, secret=arm in {"current", "ports_home"})
                if arm == "isolated_git_objects":
                    objects = workspace / ".run-objects"
                    objects.mkdir()
                    env.update(
                        {
                            "GIT_OBJECT_DIRECTORY": str(objects),
                            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(home / "owner/.git/objects"),
                        }
                    )
                for name, command, expected, output in cases(home, workspace, arm, ports):
                    start = time.monotonic()
                    result = run_command(
                        ["/usr/bin/sandbox-exec", "-p", variants[arm], "/bin/sh", "-c", command], cwd=workspace, env=env
                    )
                    seconds = time.monotonic() - start
                    assert "sandbox_apply:" not in result.stderr, result.stderr
                    success = result.returncode == 0 and (output is None or result.stdout == output)
                    control_ok = controls[name]
                    # Credential scrubbing is an environment boundary, not
                    # a kernel refusal. Other blocked probes must show one.
                    if not expected and not success and name != "inherited_credential":
                        assert _seatbelt.denied(result.stderr), (name, result.stderr)
                    rows.append(
                        {
                            "arm": arm,
                            "repeat": repeat,
                            "case": name,
                            "expected_allowed": expected,
                            "succeeded": success,
                            "control_ok": control_ok,
                            "exit_code": result.returncode,
                            "seconds": round(seconds, 4),
                            "stderr": redact(result.stderr, root) if not success else "",
                        }
                    )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return {
        "status": "tested",
        "kind": "macOS kernel; real offline installs/builds; synthetic home, keys and control API",
        "rows": rows,
        "summary": {
            arm: {
                "legitimate_failures": sum(
                    r["expected_allowed"] and not r["succeeded"] for r in rows if r["arm"] == arm
                ),
                "boundary_failures": sum(not r["expected_allowed"] and r["succeeded"] for r in rows if r["arm"] == arm),
                "cases": sum(r["arm"] == arm for r in rows),
            }
            for arm in ARMS
        },
    }


def vendor(root: Path) -> dict:
    """Installed Codex kernel executor, no model, no auth or inference needed."""
    from coding_agents.confinement import codex_filesystem
    from coding_agents.models import Mode

    if not shutil.which("codex"):
        return {"status": "unavailable"}
    directory = root / "vendor"
    directory.mkdir()
    home, workspace, cache, temporary, admin, env = fixture(directory)
    snapshot = codex_filesystem(str(workspace), Mode.EDIT, (), home=home)
    (home / "late-secret").write_text("synthetic-file-token")
    closed = {str(home): "deny", str(workspace): "write"}
    with_git = {**closed, str(home / "owner/.git"): "read", str(admin): "write", str(workspace / ".git"): "read"}
    private_copy = home / "private-copy"
    clone = run_command(
        ["git", "clone", "-q", "--no-hardlinks", str(home / "owner"), str(private_copy)], cwd=workspace, env=env
    )
    assert clone.returncode == 0, clone.stderr
    (private_copy / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")
    (private_copy / "secret-link").symlink_to(home / "existing-secret")
    private_rules = {str(home): "deny", str(private_copy): "write"}
    jobs = [
        ("workspace_read", ["/bin/cat", str(workspace / "calc.py")], True),
        ("git_status", ["git", "status", "--porcelain"], True),
        ("git_stage_and_read", ["/bin/sh", "-c", "git add calc.py && git show :calc.py"], True),
        ("existing_secret", ["/bin/cat", str(home / "existing-secret")], False),
        ("late_secret", ["/bin/cat", str(home / "late-secret")], False),
        ("symlink_read_escape", ["/bin/cat", str(workspace / "secret-link")], False),
    ]
    rows = []
    for arm, filesystem in (
        ("current_snapshot", snapshot),
        ("closed_parent", closed),
        ("closed_parent_git_roots", with_git),
        ("closed_parent_private_copy", private_rules),
        ("closed_parent_private_git_root", {**private_rules, str(private_copy / ".git"): "write"}),
    ):
        selected_workspace = private_copy if arm.startswith("closed_parent_private_") else workspace
        selected_env = dict(env)
        if arm == "closed_parent_git_roots":
            objects = workspace / ".run-objects"
            objects.mkdir()
            selected_env.update(
                {
                    "GIT_OBJECT_DIRECTORY": str(objects),
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(home / "owner/.git/objects"),
                }
            )
        inline = "{" + ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in filesystem.items()) + "}"
        for name, command, expected in jobs:
            argv = [
                "codex",
                "sandbox",
                "-C",
                str(selected_workspace),
                "-P",
                "north_trial",
                "-c",
                'permissions.north_trial.extends=":workspace"',
                "-c",
                f"permissions.north_trial.filesystem={inline}",
                *(str(value).replace(str(workspace), str(selected_workspace)) for value in command),
            ]
            result = run_command(argv, cwd=selected_workspace, env=selected_env, timeout=20)
            succeeded = result.returncode == 0
            if name in {"workspace_read", "git_stage_and_read"}:
                succeeded = succeeded and result.stdout == "def add(a, b):\n    return a + b + 0\n"
            elif name in {"existing_secret", "late_secret", "symlink_read_escape"}:
                succeeded = succeeded and result.stdout == "synthetic-file-token"
            # Invalid profiles are unavailable comparisons, not safety wins.
            config_error = any(
                word in result.stderr.lower()
                for word in (
                    "error parsing",
                    "unknown variant",
                    "unknown field",
                    "failed to load",
                    "invalid permissions",
                )
            )
            rows.append(
                {
                    "arm": arm,
                    "case": name,
                    "expected_allowed": expected,
                    "exit_code": result.returncode,
                    "succeeded": succeeded,
                    "config_error": config_error,
                    "stderr": redact(result.stderr, root) if result.returncode else "",
                }
            )
    return {
        "status": "configuration_unavailable" if any(r["config_error"] for r in rows) else "tested",
        "kind": "installed Codex sandbox command, NOT model-selected tools, file-edit tools or approval hooks",
        "version": subprocess.check_output(["codex", "--version"], text=True).strip(),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "sandbox_validation_results.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="north-sandbox-validation-") as temporary:
        root = Path(temporary).resolve()
        os.environ["NORTH_HOME"] = str(root / "isolated-north")
        results = {"sandbox": suite(root)}
        vendor_runs = []
        for repeat in range(3):
            directory = root / f"vendor-repeat-{repeat}"
            directory.mkdir()
            measured = vendor(directory)
            for row in measured.get("rows", []):
                row["repeat"] = repeat
            vendor_runs.append(measured)
        results["codex_executor"] = {
            **{key: value for key, value in vendor_runs[0].items() if key != "rows"},
            "rows": [row for run in vendor_runs for row in run.get("rows", [])],
        }
        vendor_result = results["codex_executor"]
        if any(run["status"] != "tested" for run in vendor_runs):
            vendor_result["status"] = "comparison_unavailable"
        vendor_result["summary"] = {
            arm: {
                "legitimate_failures": sum(
                    r["expected_allowed"] and not r["succeeded"] for r in vendor_result["rows"] if r["arm"] == arm
                ),
                "boundary_failures": sum(
                    not r["expected_allowed"] and r["succeeded"] for r in vendor_result["rows"] if r["arm"] == arm
                ),
                "cases": sum(r["arm"] == arm for r in vendor_result["rows"]),
            }
            for arm in dict.fromkeys(row["arm"] for row in vendor_result["rows"])
        }
    results["source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(
        json.dumps(
            {
                "sandbox": results["sandbox"].get("summary", results["sandbox"]["status"]),
                "codex_executor": {key: value for key, value in results["codex_executor"].items() if key != "rows"},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
